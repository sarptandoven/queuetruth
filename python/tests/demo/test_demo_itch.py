"""`queuetruth.demo.itch.ItchCutFilter`: the subset equals an independent scalar reference (locate selection after
the symbol's `R`, locate 0 kept, everything from the first message at or after the cut dropped) for every chunking,
on the vectorized path and on the scalar fallback."""

from __future__ import annotations

import io
import random
import struct

import demo_fixtures as F
import pytest

from queuetruth.demo.itch import ItchCutFilter

SYMS = ["AAPL", "MSFT"]
NEVER = 30 * 3600 * F.NS  # past any timestamp of a day


def reference(data: bytes, symbols: list[str], cut_ns: int) -> tuple[bytes, bool]:
    """(kept frames, cut reached) by a plain walk of the framing."""
    want = {s.encode().ljust(8) for s in symbols}
    keep = {0}
    out = bytearray()
    pos = 0
    while pos + 2 <= len(data):
        ln = struct.unpack_from(">H", data, pos)[0]
        end = pos + 2 + ln
        if end > len(data):
            break
        m = data[pos + 2 : end]
        if ln >= 11 and int.from_bytes(m[5:11], "big") >= cut_ns:
            return bytes(out), True
        if ln >= 3:
            loc = struct.unpack_from(">H", m, 1)[0]
            if m[:1] == b"R" and ln >= 19 and m[11:19] in want and loc and loc not in keep:
                keep.add(loc)
            if loc in keep:
                out += data[pos:end]
        elif ln > 0:
            out += data[pos:end]
        pos = end
    return bytes(out), False


def run(
    data: bytes, symbols: list[str], cut_ns: int, step: int | None, fast: bool = True
) -> tuple[ItchCutFilter, bytes]:
    buf = io.BytesIO()
    f = ItchCutFilter(symbols, cut_ns, buf)
    if not fast:
        f._scan_fast = lambda b: None  # type: ignore[method-assign]
    else:
        orig = f._scan_fast
        f.fast_hits = 0  # type: ignore[attr-defined]

        def spy(b: bytes) -> tuple[int, list[bytes]] | None:
            r = orig(b)
            f.fast_hits += r is not None  # type: ignore[attr-defined]
            return r

        f._scan_fast = spy  # type: ignore[method-assign]
    rng = random.Random(step or 1)
    i = 0
    while i < len(data):
        n = step if step else rng.randint(1, 70_000)
        if f.feed(data[i : i + n]):
            break
        i += n
    return f, buf.getvalue()


@pytest.mark.parametrize("step", [None, 1 << 20, 4096, 977])
@pytest.mark.parametrize("cut", [F.hms(9, 36), F.hms(9, 41, 10), F.hms(23, 0)])
def test_filter_equals_reference(itch_bytes: bytes, step: int | None, cut: int) -> None:
    want, reached = reference(itch_bytes, SYMS, cut)
    f, got = run(itch_bytes, SYMS, cut, step)
    assert got == want
    st = f.finish()
    assert st["cut_reached"] is reached
    assert st["missing_symbols"] == []
    assert st["locates"] == {"AAPL": 1, "MSFT": 2}
    assert st["kept_messages"] == sum(st["kept_by_type"].values())
    assert sum(st["kept_by_symbol"].values()) + st["kept_by_type"].get("S", 0) == st["kept_messages"]
    assert st["system_events"]["O"] == F.hms(3, 0) and st["system_events"]["Q"] == F.hms(9, 30)
    if reached:
        assert st["last_ts_ns"] < cut


def test_fast_and_scalar_paths_agree(itch_bytes: bytes) -> None:
    cut = F.hms(9, 40)
    a, ga = run(itch_bytes, SYMS, cut, 300_000, fast=True)
    b, gb = run(itch_bytes, SYMS, cut, 300_000, fast=False)
    assert ga == gb
    assert a.finish() == b.finish()
    assert a.fast_hits > 0  # type: ignore[attr-defined]


def test_cut_is_exact_at_the_boundary() -> None:
    frames = [F.itch_directory("AAPL", 1, 100)]
    frames += [F._ih("D", 1, t) + struct.pack(">Q", t) for t in (500, 999, 1000, 1001, 400)]
    data = b"".join(F.itch_frame(m) for m in frames)
    f, got = run(data, ["AAPL"], 1000, 3)
    kept = reference(data, ["AAPL"], 1000)[0]
    assert got == kept
    assert f.finish()["kept_messages"] == 3  # R, 500, 999: the message at 1000 ends the subset, later ones too
    assert f.cut_reached


def test_messages_before_the_directory_entry_are_dropped() -> None:
    frames = [
        F._ih("D", 7, 10) + struct.pack(">Q", 1),  # locate 7 before its R: not selected yet
        F.itch_directory("AAPL", 7, 20),
        F._ih("D", 7, 30) + struct.pack(">Q", 2),
        F.itch_directory("AAPL", 9, 40),  # a second R for the symbol: reported, the first locate stays
        F._ih("D", 9, 50) + struct.pack(">Q", 3),
        F.itch_system("Q", 60),
    ]
    data = b"".join(F.itch_frame(m) for m in frames)
    for fast in (True, False):
        f, got = run(data, ["AAPL"], NEVER, None, fast=fast)
        st = f.finish()
        assert got == b"".join(F.itch_frame(m) for m in (frames[1], frames[2], frames[5]))
        assert st["locates"] == {"AAPL": 7} and st["duplicate_directory"] == ["AAPL"]
        assert st["by_type"]["D"] == 3 and st["kept_by_type"] == {"R": 1, "D": 1, "S": 1}


def test_odd_frames_fall_back_to_the_scalar_path() -> None:
    frames = [F.itch_directory("AAPL", 1, 1), F._ih("D", 1, 2) + struct.pack(">Q", 1)]
    data = b"".join(F.itch_frame(m) for m in frames)
    data += b"\x00\x00"  # zero-length frame: skipped
    data += b"\x00\x02Zq"  # short frame: kept (no locate to select on)
    data += F.itch_frame(b"z" + struct.pack(">HH", 1, 0) + (3).to_bytes(6, "big") + b"unknown type")  # kept, locate 1
    data += F.itch_frame(F._ih("D", 2, 4) + struct.pack(">Q", 9))  # other locate: dropped
    f = ItchCutFilter(["AAPL"], NEVER, io.BytesIO())
    assert f._scan_fast(data) is None  # the chain leaves the candidate set
    f2, got = run(data, ["AAPL"], NEVER, None)
    assert got == reference(data, ["AAPL"], NEVER)[0]
    st = f2.finish()
    assert st["zero_frames"] == 1 and st["short_frames"] == 1


def test_missing_symbol_and_partial_frame_are_reported(itch_bytes: bytes) -> None:
    data = itch_bytes[: len(itch_bytes) // 3] + b"\x00\x24A"  # ends inside a frame
    f, _ = run(data, ["AAPL", "NOPE"], NEVER, 65_536)
    st = f.finish()
    assert st["missing_symbols"] == ["NOPE"]
    assert st["cut_reached"] is False
    assert st["partial_frame_bytes"] > 0


def test_counts_cover_every_message_read(itch_bytes: bytes) -> None:
    f, _ = run(itch_bytes, SYMS, NEVER, 1 << 16)
    st = f.finish()
    n = 0
    pos = 0
    while pos + 2 <= len(itch_bytes):
        pos += 2 + struct.unpack_from(">H", itch_bytes, pos)[0]
        n += 1
    assert st["messages_read"] == n == sum(st["by_type"].values())
    assert st["directory_entries"] == 3  # AAPL, MSFT and the unrequested ZZZT
