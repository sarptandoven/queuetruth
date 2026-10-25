"""Exclusive inter-process lock on `<ledger>.lock` (POSIX `flock`, Windows `msvcrt.locking`), plus an in-process
lock so that threads of one process also take turns."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from queuetruth.errors import QtError

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

#: Default wait for the lock (seconds); `QT_LEDGER_LOCK_TIMEOUT_S` overrides.
DEFAULT_TIMEOUT_S = 30.0

_THREAD_LOCK = threading.Lock()


def _reset_after_fork() -> None:
    """A forked child starts with a fresh thread lock: the parent's may have been held by another thread at the fork,
    and that thread does not exist in the child to release it. (A file lock held at the fork belongs to the parent's
    open file description and is released when the parent's append ends; the child opens the lock file anew.)"""
    global _THREAD_LOCK
    _THREAD_LOCK = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)


def lock_timeout_s() -> float:
    """`QT_LEDGER_LOCK_TIMEOUT_S` (a non-negative number of seconds) or the default."""
    raw = os.environ.get("QT_LEDGER_LOCK_TIMEOUT_S")
    if raw is None or not raw.strip():
        return DEFAULT_TIMEOUT_S
    try:
        v = float(raw)
    except ValueError:
        v = -1.0
    if not v >= 0:
        raise QtError("QT-CONFIG-001", f"QT_LEDGER_LOCK_TIMEOUT_S: {raw!r} is not a non-negative number of seconds")
    return v


def _try_lock(fd: int) -> bool:
    if fcntl is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, PermissionError):
            return False
        return True
    import msvcrt  # pragma: no cover - Windows

    try:  # pragma: no cover - Windows
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:  # pragma: no cover - Windows
        return False
    return True  # pragma: no cover - Windows


def _unlock(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    import msvcrt  # pragma: no cover - Windows

    os.lseek(fd, 0, os.SEEK_SET)  # pragma: no cover - Windows
    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # pragma: no cover - Windows


@contextmanager
def exclusive(path: Path, timeout_s: float | None = None) -> Iterator[None]:
    """Holds the exclusive lock on `path` (created if missing) for the block; `QT-LEDGER-002` after `timeout_s`
    seconds of waiting (default: `lock_timeout_s()`)."""
    timeout = lock_timeout_s() if timeout_s is None else timeout_s
    deadline = time.monotonic() + timeout
    if not _THREAD_LOCK.acquire(timeout=timeout if timeout > 0 else 0):
        raise _timeout(path, timeout)
    try:
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as e:
            raise QtError("QT-LEDGER-002", f"{path}: cannot open the ledger lock: {e.strerror or e}") from None
        try:
            delay = 0.001
            while not _try_lock(fd):
                now = time.monotonic()
                if now >= deadline:
                    raise _timeout(path, timeout)
                time.sleep(min(delay, deadline - now))
                delay = min(delay * 2, 0.05)
            try:
                yield
            finally:
                _unlock(fd)
        finally:
            os.close(fd)
    finally:
        _THREAD_LOCK.release()


def _timeout(path: Path, timeout: float) -> QtError:
    return QtError(
        "QT-LEDGER-002",
        f"{path}: another process held the ledger lock for more than {timeout:g} s",
        "a run is still recording; retry, or raise QT_LEDGER_LOCK_TIMEOUT_S",
    )
