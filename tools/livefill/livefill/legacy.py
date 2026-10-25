"""Converts a JSONL capture directory (one feed message per line with a top-level `recv_ns`, the layout of
`queuetruth ingest --format bitstamp` and of the predecessor's `capture_bitstamp.py`) into a livefill Parquet capture,
re-running the gap detection of `capture.CaptureSession` on it.

Files are merged by `recv_ns` (then file order). The stored `raw` is the line without its `recv_ns` member,
re-serialised compactly, so it is not byte-identical to what the venue sent (decimal strings and integer ids are
unchanged; JSON floats keep Python's shortest repr). Lines `{"event": "_capture", ...}` become connection
boundaries (`reconnect` gaps)."""

from __future__ import annotations

import gzip
import heapq
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from livefill.capture import CaptureConfig, CaptureSession
from queuetruth.errors import QtError

FILES = ("live_orders", "live_trades", "detail_order_book")


def _lines(path: Path) -> Iterator[tuple[int, int, str]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        for i, line in enumerate(f):
            if line.strip():
                yield i, 0, line


def _keyed(path: Path, rank: int) -> Iterator[tuple[int, int, int, dict[str, Any]]]:
    for i, _, line in _lines(path):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise QtError("QT-INPUT-002", f"{path} line {i + 1}: not JSON ({e.msg})") from e
        if not isinstance(obj, dict) or not isinstance(obj.get("recv_ns"), int):
            raise QtError("QT-INPUT-002", f"{path} line {i + 1}: expected an object with an integer recv_ns")
        yield obj["recv_ns"], rank, i, obj


def import_jsonl(src: str | Path, out: str | Path, markets: list[str] | None = None) -> dict[str, Any]:
    """Builds a Parquet capture at `out` from the JSONL capture directory `src`; returns its `capture_meta.json`."""
    src = Path(src)
    files = []
    for rank, stem in enumerate(FILES):
        for ext in (".jsonl", ".jsonl.gz"):
            p = src / f"{stem}{ext}"
            if p.exists():
                files.append((p, rank))
    if not files:
        raise QtError("QT-INPUT-001", f"{src}: no live_orders / live_trades / detail_order_book .jsonl files")
    if markets is None:
        meta_p = src / "capture_meta.json"
        if meta_p.exists():
            markets = list(json.loads(meta_p.read_text()).get("markets") or [])
    merged = list(heapq.merge(*(_keyed(p, r) for p, r in files), key=lambda x: (x[0], x[1], x[2])))
    if not markets:
        found = {
            str(obj.get("channel")).rpartition("_")[2]
            for _, _, _, obj in merged
            if "_" in str(obj.get("channel") or "")
        }
        markets = sorted(found)
    now = [merged[0][0] if merged else 0]
    cfg = CaptureConfig(out=Path(out), markets=markets, rest_snapshot=False)
    session = CaptureSession(cfg, clock=lambda: now[0], mono=lambda: now[0])
    session.connected()
    for recv, _, _, obj in merged:
        now[0] = recv
        del obj["recv_ns"]
        if obj.get("event") == "_capture":
            session.disconnected("capture marker in the source")
            session.connected()
            continue
        session.handle(json.dumps(obj, separators=(",", ":")), recv_ns=recv, mono_ns=recv)
    return session.close()
