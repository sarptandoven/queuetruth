"""Child-process executor (SPEC 8.1): each job's computation runs in a fresh Python process (`python -I`) with the
job's private work directory as cwd, a per-kind timeout (`Settings.job_timeout_for`: `QT_JOB_TIMEOUTS_S`, else
`QT_JOB_TIMEOUT_S`), `RLIMIT_AS` on Linux (no-op on macOS), no core dumps and, in zero-egress mode, the `netguard`
allow list. The parent SIGKILLs the child's whole process group on cancel or timeout
(the child is a session leader).

Security review M3/M2/L6: the child starts with a minimal environment (`child_env`: no `QT_*` settings, so no
database DSNs, S3 keys, pepper, signing or Stripe keys; `HOME`/`TMPDIR` point into the job directory), archive
inputs are extracted *in the child* (`runners.apply_extractions`) so extraction is bound by the same memory limit,
timeout and cancel as the computation, and an audit hook refuses `pickle` global lookups, so no job can unpickle a
user-supplied object.

The child target is `"module:function"`, called as `fn(spec: dict) -> dict` (the summary document). The request
travels as JSON on stdin and the result as JSON on a dedicated pipe. Errors cross the process boundary as
`{code, message}`: `QtError` (and `ArchiveError`) keep their code and message; any other exception becomes
`QT-ENGINE-001` with only its type name (messages may carry data), except `NotImplementedError`.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Any

CANCELED = "canceled"
TIMEOUT = "timeout"

#: Environment variables a job child inherits (everything else, including every `QT_*` setting, is dropped).
CHILD_ENV_ALLOW = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "SYSTEMROOT")

_BOOT = "from queuetruth_service.executor import _child_main; _child_main()"


@dataclass
class ChildResult:
    """Outcome of one child run."""

    status: str  # ok | error | canceled | timeout | crashed
    summary: dict[str, Any] | None = None
    error: dict[str, str] | None = None
    cpu_ms: int = 0
    wall_s: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


def child_env(workdir: str) -> dict[str, str]:
    """The complete environment of a job child: an allow list of locale/path variables plus job-private dirs."""
    env = {k: v for k, v in os.environ.items() if k in CHILD_ENV_ALLOW}
    tmp = os.path.join(workdir, "tmp")
    env.update(
        {
            "HOME": workdir,
            "TMPDIR": tmp,
            "TMP": tmp,
            "TEMP": tmp,
            "MPLCONFIGDIR": tmp,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return env


# ---- child side -------------------------------------------------------------------------------------------------


def _limit_resources(mem_bytes: int | None) -> None:
    import resource

    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (ValueError, OSError):
        pass
    if mem_bytes and sys.platform.startswith("linux"):
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))


def _cpu_ms() -> int:
    import resource

    ru = resource.getrusage(resource.RUSAGE_SELF)
    return int((ru.ru_utime + ru.ru_stime) * 1000)


def _no_unpickling(event: str, args: tuple[Any, ...]) -> None:
    if event == "pickle.find_class":
        raise RuntimeError("unpickling is disabled in job processes")


def _error_of(e: BaseException) -> dict[str, str]:
    code = getattr(e, "code", None)
    message = getattr(e, "message", None)
    if isinstance(code, str) and code.startswith("QT-") and isinstance(message, str):
        return {"code": code, "message": message}
    if isinstance(e, NotImplementedError):
        return {"code": "QT-ENGINE-001", "message": f"not implemented: {e}"}
    if isinstance(e, MemoryError):
        return {"code": "QT-ENGINE-001", "message": "out of memory"}
    # frames and the exception type only: messages may carry data (SPEC 8.10)
    sys.stderr.write("".join(traceback.format_tb(e.__traceback__)) + f"{type(e).__name__}\n")
    return {"code": "QT-ENGINE-001", "message": f"internal error ({type(e).__name__})"}


def _child_entry(req: dict[str, Any]) -> dict[str, Any]:
    """Runs in the child: limits, guards, archive extraction, then the target. Returns the result message."""
    try:
        os.chdir(req["workdir"])
        _limit_resources(req.get("mem_bytes"))
        sys.addaudithook(_no_unpickling)
        if req.get("allow") is not None:
            from queuetruth_service import netguard

            netguard.install([(str(h), int(p)) for h, p in req["allow"]])
        import importlib

        spec = req["spec"]
        if req.get("extract"):
            from queuetruth_service.runners import apply_extractions

            spec = apply_extractions(spec, req["extract"])
        module, _, name = str(req["target"]).partition(":")
        fn = getattr(importlib.import_module(module), name)
        summary = fn(spec)
        return {"status": "ok", "summary": summary, "cpu_ms": _cpu_ms()}
    except BaseException as e:
        return {"status": "error", "error": _error_of(e), "cpu_ms": _cpu_ms()}


def _child_main() -> None:
    """Entry point of the child interpreter (`python -I -c _BOOT`)."""
    req = json.loads(sys.stdin.buffer.read())
    out = os.fdopen(int(req["result_fd"]), "wb")
    sys.path[:0] = [p for p in req.get("sys_path", []) if p and p not in sys.path]
    msg = _child_entry(req)
    try:
        data = json.dumps(msg, default=str).encode()
    except (TypeError, ValueError) as e:
        data = json.dumps({"status": "error", "error": _error_of(e), "cpu_ms": _cpu_ms()}).encode()
    try:
        out.write(data)
        out.close()
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)


# ---- parent side ------------------------------------------------------------------------------------------------


def _read_all(fd: int) -> bytes:
    chunks = []
    try:
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks)


def _feed(proc: subprocess.Popen, data: bytes) -> None:
    assert proc.stdin is not None
    try:
        proc.stdin.write(data)
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass


def _kill_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


async def run_child(
    target: str,
    spec: dict[str, Any],
    workdir: str,
    *,
    timeout_s: float,
    cancel: asyncio.Event | None = None,
    allow: list[tuple[str, int]] | None = None,
    mem_bytes: int | None = None,
    extract: list[dict[str, str]] | None = None,
    poll_s: float = 0.05,
) -> ChildResult:
    """Runs `target(spec)` in a fresh interpreter with a minimal environment; kills its process group on cancel or
    timeout. `extract` lists archives the child unpacks before the target runs (`runners.apply_extractions`)."""
    os.makedirs(os.path.join(workdir, "tmp"), exist_ok=True)
    rfd, wfd = os.pipe()
    req = {
        "target": target,
        "spec": spec,
        "workdir": workdir,
        "allow": [list(a) for a in allow] if allow is not None else None,
        "mem_bytes": mem_bytes,
        "extract": extract or [],
        "sys_path": [p for p in sys.path if p and os.path.isabs(p)],  # never the job directory
        "result_fd": wfd,
    }
    payload = json.dumps(req).encode()
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(
            [sys.executable, "-I", "-c", _BOOT],
            stdin=subprocess.PIPE,
            cwd=workdir,
            env=child_env(workdir),
            pass_fds=(wfd,),
            start_new_session=True,
        )
    except BaseException:
        os.close(rfd)
        raise
    finally:
        os.close(wfd)
    reader = asyncio.ensure_future(asyncio.to_thread(_read_all, rfd))
    writer = asyncio.ensure_future(asyncio.to_thread(_feed, proc, payload))
    status: str | None = None
    try:
        while not reader.done():
            if cancel is not None and cancel.is_set():
                status = CANCELED
                break
            if time.monotonic() - t0 > timeout_s:
                status = TIMEOUT
                break
            await asyncio.sleep(poll_s)
    except BaseException:  # the worker task itself was cancelled: do not leave the child running
        status = status or CANCELED
        raise
    finally:
        if status is None:
            try:  # the result pipe closed: the child is exiting
                await asyncio.to_thread(proc.wait, 10)
            except subprocess.TimeoutExpired:
                pass
        if status is not None or proc.poll() is None:
            # signal the whole group while the leader is unreaped, so its pid cannot have been reused
            _kill_group(proc, signal.SIGKILL)
        await asyncio.to_thread(proc.wait)
        raw = await reader
        await writer
    wall = time.monotonic() - t0
    if status == TIMEOUT:
        return ChildResult(TIMEOUT, error={"code": "QT-ENGINE-001", "message": "job timed out"}, wall_s=wall)
    if status == CANCELED:
        return ChildResult(CANCELED, wall_s=wall)
    msg: dict[str, Any] | None = None
    if raw:
        try:
            msg = json.loads(raw)
        except ValueError:
            msg = None
    if isinstance(msg, dict) and msg.get("status") in ("ok", "error"):
        return ChildResult(
            status=msg["status"],
            summary=msg.get("summary"),
            error=msg.get("error"),
            cpu_ms=int(msg.get("cpu_ms", 0)),
            wall_s=wall,
        )
    return ChildResult(
        "crashed",
        error={"code": "QT-ENGINE-001", "message": f"job process exited with code {proc.returncode}"},
        wall_s=wall,
    )
