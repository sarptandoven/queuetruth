"""`queuetruth.demo.iex.IexCutFilter`: symbol selection, renumbering (source gaps carried as jumps, duplicates
dropped, heartbeats ignored), the time cut, and the vectorized pcapng path against the scalar path on every
chunking."""

from __future__ import annotations

import io
import random
import struct

import demo_fixtures as F
import pytest

from queuetruth.demo.iex import TP_HEADER, Capture, CaptureError, IexCutFilter, PcapWriter, udp_payload

DATE = "2026-10-09"
MID = F.midnight_utc_ns(DATE)
NEVER = MID + 30 * 3600 * F.NS
SYMS = ["AAPL", "MSFT"]


def run(data: bytes, symbols: list[str], cut: int, step: int | None, fast: bool = True) -> tuple[IexCutFilter, bytes]:
    buf = io.BytesIO()
    f = IexCutFilter(symbols, cut, buf)
    if not fast:
        f._fast = lambda: None  # type: ignore[method-assign]
    else:
        orig = f._fast
        f.fast_hits = 0  # type: ignore[attr-defined]

        def spy() -> bool | None:
            r = orig()
            f.fast_hits += r is not None  # type: ignore[attr-defined]
            return r

        f._fast = spy  # type: ignore[method-assign]
    rng = random.Random(step or 3)
    i = 0
    while i < len(data):
        n = step if step else rng.randint(1, 90_000)
        if f.feed(data[i : i + n]):
            break
        i += n
    return f, buf.getvalue()


def segments_of(pcap: bytes) -> list[tuple[int, tuple, list[bytes]]]:
    """(capture ns, TP header fields, messages) of every packet of a capture."""
    cap = Capture()
    cap.feed(pcap)
    out = []
    for ts, lt, frame in cap.packets():
        r = udp_payload(lt, frame)
        assert not isinstance(r, str), r
        p = r[2]
        h = TP_HEADER.unpack_from(p, 0)
        msgs, i = [], 40
        while i < 40 + h[5]:
            ln = struct.unpack_from("<H", p, i)[0]
            msgs.append(p[i + 2 : i + 2 + ln])
            i += 2 + ln
        out.append((ts, h, msgs))
    return out


def _m(kind: str, sym: str, t: int = 5) -> bytes:
    if kind == "R":
        return F.iex_msg("R", MID + t, sym, oid=t)
    return F.iex_msg("a", MID + t, sym, side=ord("B"), oid=t, size=100, price=1_000_000)


def test_renumbering_gaps_duplicates_heartbeats() -> None:
    sysev = F.iex_msg("S", MID + 1, event=ord("O"))
    segs = [
        F.tp_segment(1, [sysev, F.iex_msg("D", MID + 2, "AAA"), F.iex_msg("D", MID + 2, "BBB")], MID + 10),
        F.tp_segment(4, [_m("a", "BBB"), _m("a", "AAA")], MID + 20),
        F.tp_segment(4, [_m("a", "BBB"), _m("a", "AAA")], MID + 20),  # duplicate
        F.tp_segment(5, [_m("a", "AAA", 6), _m("R", "AAA", 7)], MID + 25),  # overlaps one message
        F.tp_segment(10, [_m("R", "AAA", 8), b"\xfe" + b"\x00" * 17], MID + 30),  # 3 missing (7..9), unknown type
        F.tp_segment(12, [], MID + 40),  # heartbeat
    ]
    frames = [(MID + 1_000_000 * (k + 1), F.eth_ipv4_udp(s)) for k, s in enumerate(segs)]
    data = F.pcapng(frames)
    for fast in (True, False):
        for step in (None, 7, len(data)):
            f, out = run(data, ["AAA"], NEVER, step, fast=fast)
            st = f.finish()
            assert st["segments"] == 6 and st["heartbeats"] == 1
            assert st["duplicate_messages"] == 3  # the repeated segment (2) and the overlapping message (1)
            assert (st["sequence_gaps"], st["sequence_gap_messages"]) == (1, 3)
            assert st["kept_by_type"] == {"S": 1, "D": 1, "a": 1, "R": 2}
            assert st["unknown_types_seen"] == {"0xfe": 1}
            got = [(h[8], h[6]) for _, h, _ in segments_of(out)]
            # renumbered: 1-2 | 3 | 4 (the overlapping message dropped) | jump of 3 -> 8
            assert got == [(1, 2), (3, 1), (4, 1), (8, 1)], (fast, step, got)
            stamps = [t for t, _, _ in segments_of(out)]
            assert stamps == [MID + 1_000_000, MID + 2_000_000, MID + 4_000_000, MID + 5_000_000]


@pytest.mark.parametrize("step", [None, 1 << 20, 65_536, 4093])
@pytest.mark.parametrize("cut_s", [9 * 3600 + 36 * 60, 9 * 3600 + 41 * 60 + 10, None])
def test_fast_equals_scalar_on_a_day(events: list[F.Ev], step: int | None, cut_s: int | None) -> None:
    data = F.iex_day(DATE, SYMS, events, gap_at=500, duplicate_at=900, heartbeat_every=40)
    cut = NEVER if cut_s is None else MID + cut_s * F.NS
    a, oa = run(data, SYMS, cut, step, fast=True)
    b, ob = run(data, SYMS, cut, step, fast=False)
    assert oa == ob
    assert a.fast_hits > 0  # type: ignore[attr-defined]
    sa, sb = a.finish(), b.finish()
    sa.pop("partial_block_bytes")
    sb.pop("partial_block_bytes")
    assert sa == sb
    assert sa["cut_reached"] is (cut_s is not None)
    assert sa["sequence_gaps"] == 1 and sa["duplicate_messages"] > 0
    if cut_s is not None:
        assert sa["last_send_ns"] < cut
        assert max(h[9] for _, h, _ in segments_of(oa)) < cut


def test_two_channels_are_numbered_separately() -> None:
    segs = []
    for k in range(40):
        ch = 1 + k % 2
        seq = 1 + 2 * (k // 2)
        msgs = [_m("a", "AAA", 100 + k), _m("a", "ZZZ", 200 + k)]
        segs.append((MID + 1000 * (k + 1), F.eth_ipv4_udp(F.tp_segment(seq, msgs, MID + 1000 * (k + 1), chan=ch))))
    data = F.pcapng(segs)
    for fast in (True, False):
        f, out = run(data, ["AAA"], NEVER, 997, fast=fast)
        per = {}
        for _, h, msgs in segments_of(out):
            per.setdefault(h[3], []).append((h[8], len(msgs)))
        assert per[1] == [(k + 1, 1) for k in range(20)] and per[2] == [(k + 1, 1) for k in range(20)]
        assert [s["channel"] for s in f.finish()["sessions"]] == [1, 2]


@pytest.mark.parametrize("variant", ["pcap", "vlan", "nanosecond", "other_protocol"])
def test_layouts_off_the_fast_path_give_the_same_subset(events: list[F.Ev], variant: str) -> None:
    ev = events[:3000]
    frames = []
    for k, e in enumerate(ev):
        t = MID + e.ts
        msg = F.iex_msg("a", t, e.sym, side=ord("B"), oid=e.oid, size=100, price=1_000_000)
        proto = 0x8004 if variant == "other_protocol" and k % 50 == 0 else 0x8005
        seg = F.tp_segment(k + 1 if proto == 0x8005 else 1, [msg], t, proto=proto)
        frames.append((t, F.eth_ipv4_udp(seg, vlan=variant == "vlan")))
    if variant == "pcap":
        buf = io.BytesIO()
        w = PcapWriter(buf)
        for t, fr in frames:
            r = udp_payload(1, fr)
            assert not isinstance(r, str)
            w.write(t, r[0], r[1], r[2])
        data = buf.getvalue()
    else:
        data = F.pcapng(frames, tsresol=9 if variant == "nanosecond" else None)
    a, oa = run(data, ["AAPL"], NEVER, 50_000, fast=True)
    b, ob = run(data, ["AAPL"], NEVER, 50_000, fast=False)
    assert oa == ob and a.finish() == b.finish()
    st = a.finish()
    if variant == "other_protocol":
        assert st["other_protocol_segments"] == {"0x8004": 60}
    assert st["kept_messages"] == sum(
        1 for k, e in enumerate(ev) if e.sym == "AAPL" and not (variant == "other_protocol" and k % 50 == 0)
    )


def test_capture_timestamps_keep_their_resolution() -> None:
    t = MID + 123_456_789_123
    seg = F.tp_segment(1, [_m("a", "AAA")], t)
    for res, want in ((None, t // 1000 * 1000), (9, t)):
        _, out = run(F.pcapng([(t, F.eth_ipv4_udp(seg))], tsresol=res), ["AAA"], NEVER, None)
        assert [ts for ts, _, _ in segments_of(out)] == [want]


def test_not_a_capture() -> None:
    f = IexCutFilter(["AAA"], NEVER, None)
    with pytest.raises(CaptureError):
        f.feed(b"definitely not a pcap file at all")


def test_cut_on_the_first_segment_keeps_nothing() -> None:
    seg = F.tp_segment(1, [_m("a", "AAA")], MID + 50)
    data = F.pcapng([(MID + 50, F.eth_ipv4_udp(seg))] * 3)
    for fast in (True, False):
        f, out = run(data, ["AAA"], MID + 50, None, fast=fast)
        st = f.finish()
        assert st["cut_reached"] and st["kept_messages"] == 0 and st["segments"] == 0
        assert segments_of(out) == []
