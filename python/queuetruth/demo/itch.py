"""Streaming Nasdaq TotalView-ITCH 5.0 symbol filter with a market-time cut (the `scripts/realdata/itch_subset.py`
selection rules, moved into the package for `queuetruth demo`).

Input: the framed stream (`[u16 BE length][message]`, as in the emi.nasdaq.com day files) in arbitrary pieces.
A message is kept when its stock locate (the 2 bytes after the type byte) is 0 (system and market-wide messages:
`S`, `V`, `W`) or belongs to a requested symbol. Locates are learned from `R` (Stock Directory) messages; the `R` of
a kept symbol is kept, every other `R` is dropped. queuetruth's ITCH mapper selects by the same header locate after
the same `R`, so the subset maps to exactly the records of the full file for those symbols. All message types of a
kept locate are kept.

Speed: a vectorized fast path (numpy) walks each chunk's framing exactly: every position that can start a frame
(a zero high length byte followed by the ITCH 5.0 length of a known message type and that type) is a candidate, each
candidate points at the candidate its frame ends on, and the true chain from the chunk's first frame is expanded by
pointer doubling. A chunk whose chain leaves the candidate set (a zero-length, short, unknown or mis-sized frame)
is walked by the scalar loop instead, so both paths give the same result on every input.

Cut: the first message whose timestamp (6 bytes after locate and tracking number, nanoseconds after midnight) is at
or after `cut_ns` ends the subset; it and everything after it are not read. The subset is therefore exactly the
stream's prefix before that message, filtered, whatever the chunking of the input. Zero-length frames are skipped
(as queuetruth's reader does); frames shorter than a timestamp cannot be cut on and are kept with locate 0 rules.
"""

from __future__ import annotations

import hashlib
from typing import Any, BinaryIO

import numpy as np

from queuetruth.demo._chain import chain_from_zero
from queuetruth.demo.sources import ITCH50_LEN

T_R = ord("R")
T_S = ord("S")
MIN_TS_FRAME = 11  # type(1) locate(2) tracking(2) timestamp(6)

# type byte -> its length low byte; 1 for unknown types (never a valid frame length, see `_chain`)
_LEN_OF_TYPE = bytes(ITCH50_LEN.get(chr(i), 1) for i in range(256))
_NEVER = np.iinfo(np.int64).max


class ItchCutFilter:
    """Symbol filter and time cut over a framed ITCH 5.0 stream (module docstring)."""

    def __init__(self, symbols: list[str], cut_ns: int, out: BinaryIO | None) -> None:
        self.symbols = [s.upper() for s in symbols]
        self.want = {s.encode("ascii").ljust(8): s for s in self.symbols}
        self.cut = int(cut_ns).to_bytes(6, "big") if cut_ns < (1 << 48) else b"\xff" * 7
        self.cut_ns = int(cut_ns)
        self.out = out
        self.keep = bytearray(65536)
        self.keep[0] = 1
        self.by_type = [0] * 256
        self.kept_by_type = [0] * 256
        self.locates: dict[str, int] = {}
        self.duplicate_directory: list[str] = []
        self.directory_entries = 0
        self.system_events: dict[str, int] = {}
        self.zero_frames = 0
        self.short_frames = 0
        self.messages = 0
        self.kept = 0
        self.kept_bytes = 0
        self.kept_by_symbol: dict[str, int] = {}
        self.first_ts: int | None = None
        self.last_ts: int | None = None
        self.cut_reached = False
        self.content_sha256 = hashlib.sha256()
        self._carry = b""
        self._loc_symbol: dict[int, str] = {}

    @property
    def market_ns(self) -> int | None:
        """Timestamp of the last message read (nanoseconds after midnight)."""
        return self.last_ts

    def feed(self, data: bytes) -> bool:
        """Consumes `data`; True once the cut is reached (later input is ignored)."""
        if self.cut_reached:
            return True
        b = self._carry + data if self._carry else data
        used, kept = self._scan(b)
        self._carry = b[used:]
        if kept:
            chunk = b"".join(kept)
            self.kept += len(kept)
            self.kept_bytes += len(chunk)
            self.content_sha256.update(chunk)
            if self.out is not None:
                self.out.write(chunk)
        return self.cut_reached

    def _scan(self, b: bytes) -> tuple[int, list[bytes]]:
        fast = self._scan_fast(b)
        return fast if fast is not None else self._scan_slow(b)

    @staticmethod
    def _chain(b: bytes, arr: np.ndarray) -> tuple[np.ndarray, np.ndarray, int] | None:
        """(frame start positions, frame lengths, bytes consumed) of the complete frames from position 0, or None
        when the chain leaves the candidate set (the scalar loop handles the chunk)."""
        n = arr.size
        if n < 3:
            return np.zeros(0, np.int64), np.zeros(0, np.int64), 0
        expect = np.frombuffer(b.translate(_LEN_OF_TYPE), dtype=np.uint8)  # per byte: length if it were a type
        cand = np.flatnonzero((expect[2:] == arr[1:-1]) & (arr[:-2] == 0))
        ln = arr[cand + 1].astype(np.int64)
        path = chain_from_zero(cand, cand + 2 + ln, n, 3)
        if path is None or (path.size and int(ln[path].min()) < MIN_TS_FRAME):  # a 1-byte "frame" of an unknown type
            return None
        if path.size == 0:
            return np.zeros(0, np.int64), np.zeros(0, np.int64), 0
        pos = cand[path]
        lens = ln[path]
        return pos, lens, int(pos[-1] + 2 + lens[-1])

    def _scan_fast(self, b: bytes) -> tuple[int, list[bytes]] | None:
        arr = np.frombuffer(b, dtype=np.uint8)
        chain = self._chain(b, arr)
        if chain is None:
            return None
        pos, lens, used = chain
        if pos.size == 0:
            return used, []
        types = arr[pos + 2]
        ts = np.zeros(pos.size, np.uint64)
        for k in range(6):
            ts = (ts << np.uint64(8)) | arr[pos + 7 + k].astype(np.uint64)
        over = np.flatnonzero(ts >= np.uint64(self.cut_ns))
        if over.size:
            k = int(over[0])
            self.cut_reached = True
            pos, lens, types, ts = pos[:k], lens[:k], types[:k], ts[:k]
            used = int(pos[-1] + 2 + lens[-1]) if k else 0
            if k == 0:
                return used, []
        loc = (arr[pos + 3].astype(np.int64) << 8) | arr[pos + 4]
        idx = np.arange(pos.size)
        keep_from = np.where(np.frombuffer(self.keep, np.uint8)[loc] == 1, -1, _NEVER)
        for r in np.flatnonzero(types == T_R).tolist():
            lr = int(loc[r])
            before = self.keep[lr]
            self._directory(b, int(pos[r]), lr)
            if not before and self.keep[lr]:
                keep_from = np.where(loc == lr, r, keep_from)
        mask = keep_from <= idx
        self.by_type = (np.asarray(self.by_type, np.int64) + np.bincount(types, minlength=256)).tolist()
        kt = types[mask]
        self.kept_by_type = (np.asarray(self.kept_by_type, np.int64) + np.bincount(kt, minlength=256)).tolist()
        kpos, kloc = pos[mask], loc[mask]
        out = [b[p : p + 2 + n] for p, n in zip(kpos.tolist(), lens[mask].tolist(), strict=True)]
        for p in kpos[(kloc == 0) & (kt == T_S)].tolist():
            self.system_events[chr(b[p + 13])] = int.from_bytes(b[p + 7 : p + 13], "big")
        counts = np.bincount(kloc, minlength=65536)
        for lc in np.flatnonzero(counts[1:]).tolist():
            s = self._loc_symbol[lc + 1]
            self.kept_by_symbol[s] = self.kept_by_symbol.get(s, 0) + int(counts[lc + 1])
        self.messages += int(pos.size)
        self.last_ts = int(ts[-1])
        if self.first_ts is None:
            self.first_ts = int(ts[0])
        return used, out

    def _scan_slow(self, b: bytes) -> tuple[int, list[bytes]]:
        n = len(b)
        pos = 0
        keep, by_type, kept_by_type, cut = self.keep, self.by_type, self.kept_by_type, self.cut
        out: list[bytes] = []
        app = out.append
        msgs = 0
        last = -1
        while pos + 2 <= n:
            ln = (b[pos] << 8) | b[pos + 1]
            end = pos + 2 + ln
            if end > n:
                break
            if ln < MIN_TS_FRAME:
                if ln == 0:
                    self.zero_frames += 1
                else:
                    self.short_frames += 1
                    msgs += 1
                    by_type[b[pos + 2]] += 1
                    if ln < 3 or keep[(b[pos + 3] << 8) | b[pos + 4]]:
                        app(b[pos:end])
                pos = end
                continue
            if b[pos + 7 : pos + 13] >= cut:
                self.cut_reached = True
                break
            t = b[pos + 2]
            by_type[t] += 1
            msgs += 1
            last = pos
            loc = (b[pos + 3] << 8) | b[pos + 4]
            if t == T_R:
                self._directory(b, pos, loc)
            if keep[loc]:
                kept_by_type[t] += 1
                app(b[pos:end])
                if loc == 0 and t == T_S and ln >= 12:
                    self.system_events[chr(b[pos + 13])] = int.from_bytes(b[pos + 7 : pos + 13], "big")
                elif loc:
                    s = self._loc_symbol[loc]
                    self.kept_by_symbol[s] = self.kept_by_symbol.get(s, 0) + 1
            pos = end
        self.messages += msgs
        if last >= 0:
            ts = int.from_bytes(b[last + 7 : last + 13], "big")
            self.last_ts = ts
            if self.first_ts is None:
                self.first_ts = self._first_ts(b)
        return pos, out

    @staticmethod
    def _first_ts(b: bytes) -> int | None:
        pos = 0
        while pos + 2 <= len(b):
            ln = (b[pos] << 8) | b[pos + 1]
            if ln >= MIN_TS_FRAME and pos + 2 + ln <= len(b):
                return int.from_bytes(b[pos + 7 : pos + 13], "big")
            pos += 2 + ln
        return None

    def _directory(self, b: bytes, pos: int, loc: int) -> None:
        self.directory_entries += 1
        sym = self.want.get(bytes(b[pos + 13 : pos + 21]))
        if sym is None or loc == 0:
            return
        if sym in self.locates:
            self.duplicate_directory.append(sym)
            return
        self.locates[sym] = loc
        self._loc_symbol[loc] = sym
        self.keep[loc] = 1

    def finish(self) -> dict[str, Any]:
        """Statistics of the pass (call after the last `feed`)."""
        return {
            "messages_read": self.messages,
            "kept_messages": self.kept,
            "kept_framed_bytes": self.kept_bytes,
            "content_sha256": self.content_sha256.hexdigest(),
            "by_type": {chr(k): v for k, v in enumerate(self.by_type) if v},
            "kept_by_type": {chr(k): v for k, v in enumerate(self.kept_by_type) if v},
            "kept_by_symbol": dict(sorted(self.kept_by_symbol.items())),
            "locates": dict(sorted(self.locates.items())),
            "missing_symbols": [s for s in self.symbols if s not in self.locates],
            "duplicate_directory": self.duplicate_directory,
            "directory_entries": self.directory_entries,
            "system_events": self.system_events,
            "zero_frames": self.zero_frames,
            "short_frames": self.short_frames,
            "partial_frame_bytes": len(self._carry) if not self.cut_reached else 0,
            "first_ts_ns": self.first_ts,
            "last_ts_ns": self.last_ts,
            "cut_reached": self.cut_reached,
        }


__all__ = ["ItchCutFilter"]
