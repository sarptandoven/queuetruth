"""Streaming reader for the user's own captures (WebSocket frames, REST responses, chain-log exports).

Input files are JSON lines (one value per line, streamed), or one JSON document that spans several lines (an array
is read element by element), plain or gzipped (`.gz`, detected from the magic bytes). Each value may use the
capture wrappers of `queuetruth.live` (see the import-live-logs how-to):

- `{"recv_ns": <int>, "msg": <frame>}`: a received frame with its local receive time;
- `{"dir": "in" | "out", "local_ns": <int>, "msg": <frame>}`: a request (`out`, local send time) or a response or
  frame (`in`, local receive time).

`msg` may be the frame as a JSON value or as the raw text of the frame. A wrapper may carry context keys for REST
captures (`path`, `method`, `order_id`, `ticker`, `market_ticker`, `asset_id`, `endpoint`, `send_ns`); an `out`
record shaped like a request (`{"method": ..., "path": ...}`) is attached to the next `in` record of the same file as
`Frame.request`, and its local send time becomes that response's `send_ns`. Heartbeat text frames (`PING`, `PONG`)
are skipped and counted; any other line that is not JSON fails with `QT-INPUT-002` naming the file and line. A frame
that is a JSON array yields one `Frame` per element (Polymarket sends arrays of market events; REST endpoints return
arrays). Numbers that are not integers are parsed as `Decimal`, never as binary floats. `order_inputs` puts the
inputs in time order by each file's first local time, so rotated captures can be given in any order.
"""

from __future__ import annotations

import gzip
import io
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from queuetruth.errors import QtError

HEARTBEATS = frozenset({"PING", "PONG", "ping", "pong"})
CONTEXT_KEYS = ("path", "method", "order_id", "ticker", "market_ticker", "asset_id", "endpoint")


@dataclass
class Frame:
    """One captured message: parsed value, local times, location and wrapper context."""

    obj: Any
    location: str
    file_idx: int
    line_idx: int
    sub_idx: int = 0
    recv_ns: int | None = None
    send_ns: int | None = None
    direction: str = "in"
    context: dict[str, Any] = field(default_factory=dict)
    request: dict[str, Any] | None = None

    @property
    def pos(self) -> tuple[int, int, int]:
        """Capture position (file, line, element) for stable ordering."""
        return (self.file_idx, self.line_idx, self.sub_idx)


@dataclass
class CaptureStats:
    """Counters of one read."""

    files: int = 0
    lines: int = 0
    frames: int = 0
    heartbeats: int = 0
    requests: int = 0


def _loads(text: str) -> Any:
    return json.loads(text, parse_float=Decimal)


def _open_text(path: Path) -> io.TextIOWrapper:
    try:
        raw = path.open("rb")
    except OSError as e:
        raise QtError("QT-INPUT-001", f"{path}: {e}") from None
    head = raw.read(2)
    raw.seek(0)
    stream: Any = gzip.GzipFile(fileobj=raw) if head == b"\x1f\x8b" else raw
    return io.TextIOWrapper(stream, encoding="utf-8-sig")


def _int(v: Any, what: str, where: str) -> int:
    if isinstance(v, bool):
        raise QtError("QT-INPUT-002", f"{where}: {what} must be an integer ns timestamp, got {v!r}")
    if isinstance(v, int):
        return v
    if isinstance(v, Decimal) and v == v.to_integral_value():
        return int(v)
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    raise QtError("QT-INPUT-002", f"{where}: {what} must be an integer ns timestamp, got {v!r}")


def _unwrap(obj: Any, where: str) -> tuple[Any, int | None, int | None, str, dict[str, Any]]:
    """(frame, recv_ns, send_ns, direction, context)."""
    if not (isinstance(obj, dict) and "msg" in obj and ("recv_ns" in obj or "local_ns" in obj)):
        return obj, None, None, "in", {}
    msg = obj["msg"]
    if isinstance(msg, str):
        s = msg.strip()
        if s in HEARTBEATS:
            msg = s
        else:
            try:
                msg = _loads(s)
            except json.JSONDecodeError as e:
                raise QtError("QT-INPUT-002", f"{where}: wrapped msg is not JSON: {e.msg}") from None
    ctx = {k: obj[k] for k in CONTEXT_KEYS if k in obj}
    send = _int(obj["send_ns"], "send_ns", where) if obj.get("send_ns") is not None else None
    if "recv_ns" in obj:
        return msg, _int(obj["recv_ns"], "recv_ns", where), send, "in", ctx
    direction = str(obj.get("dir", "in")).lower()
    if direction not in ("in", "out"):
        raise QtError("QT-INPUT-002", f"{where}: capture dir must be 'in' or 'out', got {direction!r}")
    local = _int(obj["local_ns"], "local_ns", where)
    if direction == "out":
        return msg, None, local, "out", ctx
    return msg, local, send, "in", ctx


def _values(path: Path, st: CaptureStats) -> Iterator[tuple[int, Any]]:
    """(line number, JSON value) of a JSON-lines file (streamed), or the elements of a multi-line JSON document."""
    with _open_text(path) as fh:
        try:
            first: tuple[int, str] | None = None
            for n, line in enumerate(fh, start=1):
                s = line.strip()
                if not s:
                    continue
                if s in HEARTBEATS:
                    st.heartbeats += 1
                    continue
                try:
                    value = _loads(s)
                except json.JSONDecodeError as e:
                    if first is None:
                        first = (n, line)
                        break
                    raise QtError("QT-INPUT-002", f"{path}:{n}: invalid JSON line: {e.msg}") from None
                if first is None:
                    first = (n, "")
                yield n, value
            else:
                return
            # the first value spans several lines: one JSON document
            rest = fh.read()
        except (OSError, EOFError, UnicodeDecodeError) as e:
            raise QtError("QT-INPUT-002", f"{path}: unreadable capture: {e}") from None
    n0, head = first
    try:
        doc = _loads(head + rest)
    except json.JSONDecodeError as e:
        raise QtError("QT-INPUT-002", f"{path}:{n0 + e.lineno - 1}: invalid JSON document: {e.msg}") from None
    if isinstance(doc, list):
        yield from ((n0 + i, v) for i, v in enumerate(doc))
    else:
        yield n0, doc


def iter_frames(paths: list[str | Path], stats: CaptureStats | None = None) -> Iterator[Frame]:
    """Frames of every input in order (file index = position in `paths`). `out` records are yielded too
    (`direction = "out"`); a request-shaped `out` record is attached to the next `in` record of its file."""
    st = stats if stats is not None else CaptureStats()
    for fi, p in enumerate(paths):
        path = Path(p)
        if not path.is_file():
            raise QtError("QT-INPUT-001", f"{path}: input not found")
        st.files += 1
        pending: dict[str, Any] | None = None
        for n, obj in _values(path, st):
            st.lines += 1
            where = f"{path}:{n}"
            msg, recv, send, direction, ctx = _unwrap(obj, where)
            if isinstance(msg, str) and msg in HEARTBEATS:
                st.heartbeats += 1
                continue
            if direction == "out":
                st.requests += 1
                req = dict(msg) if isinstance(msg, dict) else {"raw": msg}
                req["send_ns"] = send
                pending = req if ("path" in req or "method" in req) else None
                yield Frame(msg, where, fi, n, 0, None, send, "out", ctx)
                continue
            request, pending = pending, None
            if request is not None and send is None:
                send = request.get("send_ns")
            elems = msg if isinstance(msg, list) else [msg]
            for k, m in enumerate(elems):
                st.frames += 1
                loc = where if len(elems) == 1 else f"{where}[{k}]"
                yield Frame(m, loc, fi, n, k, recv, send, "in", dict(ctx), request)


def read_frames(paths: list[str | Path]) -> tuple[list[Frame], CaptureStats]:
    """Every frame of the inputs and the read counters."""
    st = CaptureStats()
    return list(iter_frames(paths, st)), st


#: Frames read at the start of a file to find its first local time.
_FIRST_TIME_SCAN = 1000


def first_time(path: str | Path) -> int | None:
    """Local time of a capture's first timed frame (its receive time, else its send time), looking at the first
    frames only; None when they carry no local time (bare frames)."""
    for n, fr in enumerate(iter_frames([path])):
        t = fr.recv_ns if fr.recv_ns is not None else fr.send_ns
        if t is not None:
            return t
        if n + 1 >= _FIRST_TIME_SCAN:
            break
    return None


def order_inputs(paths: list[str | Path]) -> list[Path]:
    """The inputs in time order, by the local time of each file's first timed frame (stable for ties; files without
    local times follow in their given order), so rotated captures of one connection can be given in any order. A
    missing file is `QT-INPUT-001`."""
    keyed = []
    for i, p in enumerate(paths):
        t = first_time(p)
        keyed.append(((t is None, t if t is not None else 0, i), Path(p)))
    return [p for _k, p in sorted(keyed, key=lambda x: x[0])]


__all__ = ["HEARTBEATS", "CaptureStats", "Frame", "first_time", "iter_frames", "order_inputs", "read_frames"]
