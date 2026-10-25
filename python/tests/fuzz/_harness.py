"""Shared machinery of the parser fuzz suite: the outcome contract, the per-example time and memory guards, the
example budget and the statistics printed at the end of the session.

Contract (every property): a parser either returns, or raises `QtError` whose code matches `QT-<AREA>-<NNN>`. Any
other exception, an example that runs longer than `TIME_LIMIT_S`, or an example whose traced Python allocations
peak above `MEMORY_LIMIT_BYTES` fails the property. Hypothesis then shrinks the input to a minimal reproducer.

Budget: `QT_FUZZ_EXAMPLES` (default 40) is the example count of every property; `QT_FUZZ_PROFILE=deep` runs
random (not derandomized) examples, for a long local run. The default is derandomized so CI runs are reproducible.
`QT_FUZZ_STATS=<path>` also writes the per-target statistics as JSON.
"""

from __future__ import annotations

import contextlib
import faulthandler
import json
import os
import re
import resource
import signal
import sys
import tempfile
import threading
import time
import traceback
import tracemalloc
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from hypothesis import HealthCheck, settings

from queuetruth.errors import QtError

QT_CODE = re.compile(r"^QT-[A-Z]+-\d{3}$")
#: Wall-clock limit of one example (generous: the suite runs on loaded laptops; real runs take milliseconds).
TIME_LIMIT_S = float(os.environ.get("QT_FUZZ_TIME_LIMIT_S", "20"))
#: Peak of the Python allocations traced during one example.
MEMORY_LIMIT_BYTES = int(os.environ.get("QT_FUZZ_MEMORY_LIMIT_MB", "256")) * 2**20
#: Growth of the process's peak RSS during one example that kills the run (C-level allocations: see `_rss_guard`).
RSS_LIMIT_BYTES = int(os.environ.get("QT_FUZZ_RSS_LIMIT_MB", "1536")) * 2**20
#: Distinct QtError messages kept per target and code (printed with `QT_FUZZ_VERBOSE=1`).
SAMPLES = 4
VERBOSE = os.environ.get("QT_FUZZ_VERBOSE", "") not in ("", "0")
_TMP = re.compile(r"\S*qt-fuzz-[A-Za-z0-9_]+/")
EXAMPLES = max(1, int(os.environ.get("QT_FUZZ_EXAMPLES", "40")))
DEEP = os.environ.get("QT_FUZZ_PROFILE", "").lower() == "deep"

#: Settings of every property (`@FUZZ`); `scaled(k)` for properties that are k times cheaper or costlier.
FUZZ = settings(
    max_examples=EXAMPLES,
    deadline=None,
    database=None,
    derandomize=not DEEP,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large, HealthCheck.filter_too_much],
)


def scaled(k: float) -> settings:
    """`FUZZ` with `k` times the example count (at least 1)."""
    return settings(FUZZ, max_examples=max(1, int(EXAMPLES * k)))


class FuzzTimeout(BaseException):
    """Raised in the example by the watchdog (a BaseException so `except Exception` in the code under test cannot
    swallow it)."""


@dataclass
class TargetStats:
    """Per-target counters: examples, outcomes by QT code, slowest example and largest memory peak."""

    examples: int = 0
    ok: int = 0
    errors: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0
    max_seconds: float = 0.0
    max_peak_bytes: int = 0
    max_input_bytes: int = 0
    samples: dict[str, list[str]] = field(default_factory=dict)


STATS: dict[str, TargetStats] = {}


@contextlib.contextmanager
def _watchdog(seconds: float, what: str) -> Iterator[None]:
    """SIGALRM interrupts Python-level loops; faulthandler kills the process (with every thread's traceback) when C
    code holds the interpreter far past the limit (a SIGALRM handler only runs between bytecodes)."""
    use_alarm = hasattr(signal, "setitimer") and _main_thread()
    if use_alarm:

        def _fire(signum: int, frame: Any) -> None:
            raise FuzzTimeout(f"{what}: example exceeded {seconds:.0f} s")

        old = signal.signal(signal.SIGALRM, _fire)
        signal.setitimer(signal.ITIMER_REAL, seconds)
    faulthandler.dump_traceback_later(seconds * 6 + 60, exit=True)
    _arm(what)
    try:
        yield
    finally:
        _ARMED[0] = None
        faulthandler.cancel_dump_traceback_later()
        if use_alarm:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, old)


def _main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


def _max_rss() -> int:
    """Peak resident set size of the process in bytes (`ru_maxrss` is bytes on macOS, KiB on Linux)."""
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r if sys.platform == "darwin" else r * 1024


#: (target, peak RSS when the example started) while an example runs, else None; read by the guard thread.
_ARMED: list[tuple[str, int] | None] = [None]
_GUARD: list[threading.Thread] = []


def _arm(what: str) -> None:
    _ARMED[0] = (what, _max_rss())
    if not _GUARD:
        t = threading.Thread(target=_rss_guard, name="qt-fuzz-rss-guard", daemon=True)
        t.start()
        _GUARD.append(t)


def _rss_guard() -> None:
    """Backstop for allocations in C code that no Python-level check can interrupt (a decimal or integer with a
    billion digits): when the process's peak RSS grows by more than `RSS_LIMIT_BYTES` during one example, dump
    every thread's traceback and exit the process (the alternative is the OS killing it without a word). One
    daemon thread for the session, polling every 20 ms. The process exits with status 3 (faulthandler's own
    backstop for a C-level hang exits with status 1); pytest's output capture swallows the traceback, so rerun the
    failing property with `-s` to see it."""
    while True:
        time.sleep(0.02)
        armed = _ARMED[0]
        if armed is not None and _max_rss() - armed[1] > RSS_LIMIT_BYTES:
            sys.stderr.write(f"\nqueuetruth fuzz: {armed[0]}: resident memory grew past {RSS_LIMIT_BYTES >> 20} MiB\n")
            faulthandler.dump_traceback(all_threads=True)
            os._exit(3)


T = TypeVar("T")


def guarded(what: str, fn: Callable[[], T], *, input_bytes: int = 0) -> T | QtError:
    """Runs `fn` under the contract (module docstring); returns its result or the `QtError` it raised."""
    st = STATS.setdefault(what, TargetStats())
    st.examples += 1
    st.max_input_bytes = max(st.max_input_bytes, input_bytes)
    tracing = not tracemalloc.is_tracing()
    if tracing:
        tracemalloc.start()
    tracemalloc.reset_peak()
    t0 = time.perf_counter()
    outcome: T | QtError
    try:
        with _watchdog(TIME_LIMIT_S, what):
            outcome = fn()
    except QtError as e:
        if not QT_CODE.match(str(e.code)):
            raise AssertionError(f"{what}: QtError with a malformed code {e.code!r}: {e}") from e
        if not isinstance(e.message, str) or not e.message:
            raise AssertionError(f"{what}: QtError {e.code} without a message") from e
        st.errors[e.code] = st.errors.get(e.code, 0) + 1
        seen = st.samples.setdefault(e.code, [])
        msg = _TMP.sub("", e.message[:400])[:160]  # bounded first: `\S*` rescans a long token per position
        if len(seen) < SAMPLES and msg not in seen:
            seen.append(msg)
        outcome = e
    except FuzzTimeout as e:
        raise AssertionError(str(e)) from None
    except Exception as e:  # the property: nothing but QtError may escape a parser
        raise AssertionError(
            f"{what}: {type(e).__name__} escaped instead of a QtError at {_origin(e)}: {e!s:.300}"
        ) from e
    else:
        st.ok += 1
    finally:
        dt = time.perf_counter() - t0
        _, peak = tracemalloc.get_traced_memory()
        if tracing:
            tracemalloc.stop()
        st.seconds += dt
        st.max_seconds = max(st.max_seconds, dt)
        st.max_peak_bytes = max(st.max_peak_bytes, peak)
    if peak > MEMORY_LIMIT_BYTES:
        raise AssertionError(
            f"{what}: traced allocations peaked at {peak / 2**20:.1f} MiB for {input_bytes} input bytes"
        )
    return outcome


def _origin(e: BaseException) -> str:
    """`file:line in function` of the innermost traceback frame inside the queuetruth package."""
    where = "?"
    for fr in traceback.extract_tb(e.__traceback__):
        if f"{os.sep}queuetruth{os.sep}" in fr.filename:
            where = f"{fr.filename.rsplit(os.sep + 'queuetruth' + os.sep, 1)[-1]}:{fr.lineno} in {fr.name}"
    return where


@contextlib.contextmanager
def workdir() -> Iterator[Path]:
    """A fresh temporary directory per example (Hypothesis cannot use function-scoped fixtures per example)."""
    with tempfile.TemporaryDirectory(prefix="qt-fuzz-") as d:
        yield Path(d)


def write_files(root: Path, files: dict[str, bytes]) -> tuple[list[str], int]:
    """Writes `{relative name: bytes}` under `root`; returns the paths in order and the total size."""
    paths = []
    total = 0
    for name, data in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        paths.append(str(p))
        total += len(data)
    return paths, total


def stats_report() -> list[str]:
    """Lines of the end-of-session table (one per target)."""
    if not STATS:
        return []
    lines = [
        f"queuetruth parser fuzz: {EXAMPLES} examples per property ({'deep, random' if DEEP else 'derandomized'}); "
        f"limits {TIME_LIMIT_S:.0f} s and {MEMORY_LIMIT_BYTES // 2**20} MiB per example",
        f"{'target':<34} {'examples':>8} {'ok':>6} {'QtError':>8} {'total s':>8} {'max ms':>8} {'peak MiB':>9}  codes",
    ]
    for name in sorted(STATS):
        s = STATS[name]
        n_err = sum(s.errors.values())
        codes = " ".join(f"{k}={v}" for k, v in sorted(s.errors.items()))
        lines.append(
            f"{name:<34} {s.examples:>8} {s.ok:>6} {n_err:>8} {s.seconds:>8.2f} {s.max_seconds * 1000:>8.1f} "
            f"{s.max_peak_bytes / 2**20:>9.2f}  {codes}"
        )
        if VERBOSE:
            lines.extend(f"    {code}: {m}" for code, msgs in sorted(s.samples.items()) for m in msgs)
    total = sum(s.examples for s in STATS.values())
    secs = sum(s.seconds for s in STATS.values())
    lines.append(f"{'total':<34} {total:>8} {'':>6} {'':>8} {secs:>8.2f}")
    path = os.environ.get("QT_FUZZ_STATS")
    if path:
        doc = {
            "examples_per_property": EXAMPLES,
            "deep": DEEP,
            "python": sys.version.split()[0],
            "targets": {k: vars(v) for k, v in sorted(STATS.items())},
        }
        Path(path).write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return lines
