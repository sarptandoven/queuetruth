"""`queuetruth.demo.subset`: building the subset from a file or an HTTP stream (stopping at the cut), the manifest,
the cache check, the pins, reading the demo's own subsets back, the atomic swap into the cache, and every guard
(input ends before the last label, wrong format, wrong date, missing symbol, download cap, disk space)."""

from __future__ import annotations

import functools
import gzip
import hashlib
import http.server
import json
import struct
import threading
from collections.abc import Iterator
from pathlib import Path

import demo_fixtures as F
import pytest
from test_demo_itch import reference

from queuetruth.demo import subset as SB
from queuetruth.demo.fetch import CHUNK
from queuetruth.demo.iex import Capture
from queuetruth.demo.sources import DemoWindow, custom_source
from queuetruth.errors import QtError

SYMS = ["AAPL", "MSFT"]


def _itch_source(path: Path):
    return custom_source("itch", "2019-12-30", str(path), tuple(SYMS))


def test_itch_subset_from_file(tmp_path: Path, itch_gz: Path, itch_bytes: bytes) -> None:
    w = DemoWindow("2019-12-30", 1)
    seen: list[dict] = []
    r = SB.build_subset(_itch_source(itch_gz), SYMS, w, tmp_path / "c", progress=seen.append, progress_every_s=0.0)
    assert not r.cache_hit and r.path.name == "subset.itch50.gz"
    content = gzip.decompress(r.path.read_bytes())
    want, reached = reference(itch_bytes, SYMS, w.cut_ns_local)
    assert reached and content == want
    m = json.loads(r.manifest_path.read_text())
    assert m == r.manifest
    assert m["schema"] == "queuetruth.demo_subset" and m["schema_version"] == 1
    assert m["filter_version"] == SB.FILTER_VERSION and m["filter"]["complete"] == "cut"
    assert m["pinned_source"] is None
    assert m["filter"]["content_sha256"] == hashlib.sha256(content).hexdigest()
    assert m["subset"]["sha256"] == hashlib.sha256(r.path.read_bytes()).hexdigest()
    assert m["subset"]["bytes"] == r.path.stat().st_size
    assert m["source"]["bytes_read"] == r.bytes_downloaded <= itch_gz.stat().st_size
    assert m["source"]["input_encoding"] == "gzip" and m["source"]["remote"] is False
    assert m["window"]["cut"] == "09:41:10" and m["pinned"] is False
    assert m["source"]["attribution"] is None
    assert not list((tmp_path / "c").glob("*.part"))
    assert seen and all(x["cut_s"] == w.cut_s for x in seen)
    # the cache check
    hit = SB.cached(tmp_path / "c", _itch_source(itch_gz))
    assert hit is not None and hit.cache_hit and hit.bytes_downloaded == 0 and hit.path == r.path
    raw = bytearray(r.path.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    r.path.write_bytes(bytes(raw))
    assert SB.cached(tmp_path / "c", _itch_source(itch_gz)) is None


def test_cached_rejects_missing_or_foreign_manifest(tmp_path: Path, itch_gz: Path) -> None:
    d = tmp_path / "c"
    assert SB.cached(d, _itch_source(itch_gz)) is None
    d.mkdir()
    (d / "subset.itch50.gz").write_bytes(b"x")
    (d / "manifest.json").write_text("{not json")
    assert SB.cached(d, _itch_source(itch_gz)) is None
    (d / "manifest.json").write_text(json.dumps({"schema": "other", "subset": {"bytes": 1}}))
    assert SB.cached(d, _itch_source(itch_gz)) is None


def test_iex_subset_from_file_carries_attribution(tmp_path: Path, iex_gz: Path) -> None:
    src = custom_source("iex", "2026-10-09", str(iex_gz), tuple(SYMS))
    r = SB.build_subset(src, SYMS, DemoWindow("2026-10-09", 1), tmp_path / "c")
    m = r.manifest
    assert m["source"]["attribution"].startswith("Data provided for free by IEX.")
    cap = Capture()
    cap.feed(gzip.decompress(r.path.read_bytes()))
    n = sum(1 for _ in cap.packets())
    assert cap.kind == "pcap" and n == m["filter"]["kept_segments"] > 0
    assert set(m["filter"]["kept_by_symbol"]) == set(SYMS)


def test_input_that_ends_before_the_cut(tmp_path: Path, itch_bytes: bytes) -> None:
    p = tmp_path / "short.itch50"
    events = F.book_events(SYMS, F.hms(9, 30), F.hms(9, 33), seed=2)
    p.write_bytes(F.itch_day(SYMS, events, close=False))
    with pytest.raises(QtError) as e:
        SB.build_subset(_itch_source(p), SYMS, DemoWindow("2019-12-30", 1), tmp_path / "c")
    assert e.value.code == "QT-INPUT-002" and "before the end of the last label 09:41:00" in e.value.message
    assert not list((tmp_path / "c").iterdir())


def test_input_that_ends_after_the_last_label_is_complete(tmp_path: Path) -> None:
    w = DemoWindow("2019-12-30", 1)  # last label ends 09:41:00, cut 09:41:10
    events = F.book_events(SYMS, F.hms(9, 30), F.hms(9, 41, 5), seed=2)
    data = F.itch_day(SYMS, events, close=False)
    p = tmp_path / "prefix.itch50"
    p.write_bytes(data)
    r = SB.build_subset(_itch_source(p), SYMS, w, tmp_path / "c")
    f = r.manifest["filter"]
    assert not f["cut_reached"] and f["complete"] == "end_of_input" and f["last_ts_ns"] >= w.complete_s * 10**9
    assert gzip.decompress(r.path.read_bytes()) == reference(data, SYMS, w.cut_ns_local)[0]


@pytest.mark.parametrize("fmt", ["itch", "iex"])
def test_the_demos_own_subset_reads_back_to_the_same_content(
    fmt: str, tmp_path: Path, itch_gz: Path, iex_gz: Path
) -> None:
    date = "2019-12-30" if fmt == "itch" else "2026-10-09"
    w = DemoWindow(date, 1)
    first = SB.build_subset(
        custom_source(fmt, date, str(itch_gz if fmt == "itch" else iex_gz), tuple(SYMS)), SYMS, w, tmp_path / "a"
    )
    again = SB.build_subset(custom_source(fmt, date, str(first.path), tuple(SYMS)), SYMS, w, tmp_path / "b")
    f1, f2 = first.manifest["filter"], again.manifest["filter"]
    assert f1["complete"] == "cut" and f2["complete"] == "end_of_input"
    assert (f2["content_sha256"], f2["kept_messages"]) == (f1["content_sha256"], f1["kept_messages"])
    assert gzip.decompress(again.path.read_bytes()) == gzip.decompress(first.path.read_bytes())


def test_content_that_matches_a_pin_is_reported_as_that_source(
    tmp_path: Path, itch_gz: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = DemoWindow("2019-12-30", 1)
    built = SB.build_subset(_itch_source(itch_gz), SYMS, w, tmp_path / "a")
    f = built.manifest["filter"]
    monkeypatch.setitem(SB.PINS, ("nasdaq-2019-12-30", tuple(SYMS), w.tag()), (f["content_sha256"], f["kept_messages"]))
    again = SB.build_subset(_itch_source(built.path), SYMS, w, tmp_path / "b")
    assert again.manifest["pinned"] is True and again.manifest["pinned_source"] == "nasdaq-2019-12-30"
    # another session date is another day, whatever the bytes
    other = custom_source("itch", "2019-12-31", str(built.path), tuple(SYMS))
    assert SB.build_subset(other, SYMS, DemoWindow("2019-12-31", 1), tmp_path / "c").manifest["pinned"] is False


def test_wrong_format_is_named(tmp_path: Path, itch_gz: Path, iex_gz: Path) -> None:
    with pytest.raises(QtError) as e:
        SB.build_subset(
            custom_source("iex", "2026-10-09", str(itch_gz), tuple(SYMS)),
            SYMS,
            DemoWindow("2026-10-09", 1),
            tmp_path / "a",
        )
    assert e.value.code == "QT-INPUT-002" and "--input-format itch" in (e.value.hint or "")
    with pytest.raises(QtError) as e:
        SB.build_subset(
            custom_source("itch", "2019-12-30", str(iex_gz), tuple(SYMS)),
            SYMS,
            DemoWindow("2019-12-30", 1),
            tmp_path / "b",
        )
    assert e.value.code == "QT-INPUT-002" and "--input-format iex" in (e.value.hint or "")


@pytest.mark.parametrize("date", ["2026-10-08", "2026-10-10"])
def test_iex_capture_of_another_day_stops_at_once(date: str, tmp_path: Path, iex_gz: Path) -> None:
    src = custom_source("iex", date, str(iex_gz), tuple(SYMS))
    with pytest.raises(QtError) as e:
        SB.build_subset(src, SYMS, DemoWindow(date, 1), tmp_path / "a")
    assert e.value.code == "QT-INPUT-002"
    assert f"starts on 2026-10-09 (US/Eastern), not on the session date {date}" in e.value.message


def test_cached_rejects_old_filter_versions_and_broken_pins(
    tmp_path: Path, itch_gz: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = DemoWindow("2019-12-30", 1)
    src = _itch_source(itch_gz)
    r = SB.build_subset(src, SYMS, w, tmp_path / "c")
    assert SB.cached(tmp_path / "c", src, SYMS, w) is not None
    monkeypatch.setitem(SB.PINS, ("custom", tuple(SYMS), w.tag()), ("0" * 64, 1))
    assert SB.cached(tmp_path / "c", src, SYMS, w) is None
    monkeypatch.delitem(SB.PINS, ("custom", tuple(SYMS), w.tag()))
    monkeypatch.setattr(SB, "FILTER_VERSION", SB.FILTER_VERSION + 1)
    assert SB.cached(tmp_path / "c", src, SYMS, w) is None
    monkeypatch.undo()
    m = json.loads(r.manifest_path.read_text())
    m["schema_version"] = 2
    r.manifest_path.write_text(json.dumps(m))
    assert SB.cached(tmp_path / "c", src, SYMS, w) is None


def test_fetch_subset_swaps_in_only_on_success(tmp_path: Path, itch_gz: Path) -> None:
    w = DemoWindow("2019-12-30", 1)
    entry = tmp_path / "cache" / "k"
    good = SB.fetch_subset(_itch_source(itch_gz), SYMS, w, entry)
    assert good.path == entry / "subset.itch50.gz" and SB.cached(entry, _itch_source(itch_gz), SYMS, w) is not None
    before = good.path.read_bytes()
    # a refresh that fails (a symbol with no message) leaves the good entry as it was, and no build directory
    with pytest.raises(QtError):
        SB.fetch_subset(_itch_source(itch_gz), ["AAPL", "NOPE"], w, entry)
    assert good.path.read_bytes() == before and SB.cached(entry, _itch_source(itch_gz), SYMS, w) is not None
    again = SB.fetch_subset(_itch_source(itch_gz), SYMS, w, entry)
    assert again.path == good.path and json.loads(again.manifest_path.read_text()) == again.manifest
    assert sorted(p.name for p in (tmp_path / "cache").iterdir()) == ["k"]


def test_missing_symbol(tmp_path: Path, itch_gz: Path) -> None:
    with pytest.raises(QtError) as e:
        SB.build_subset(_itch_source(itch_gz), ["AAPL", "NOPE"], DemoWindow("2019-12-30", 1), tmp_path / "c")
    assert e.value.code == "QT-INPUT-002" and "NOPE" in e.value.message
    assert not list((tmp_path / "c").iterdir())


def test_pins(tmp_path: Path, itch_gz: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = DemoWindow("2019-12-30", 1)
    key = ("custom", tuple(SYMS), w.tag())
    monkeypatch.setitem(SB.PINS, key, ("0" * 64, 1))
    with pytest.raises(QtError) as e:
        SB.build_subset(_itch_source(itch_gz), SYMS, w, tmp_path / "a")
    assert e.value.code == "QT-INPUT-002" and "pinned" in e.value.message
    assert not list((tmp_path / "a").iterdir())
    monkeypatch.delitem(SB.PINS, key)
    ok = SB.build_subset(_itch_source(itch_gz), SYMS, w, tmp_path / "b")
    f = ok.manifest["filter"]
    monkeypatch.setitem(SB.PINS, key, (f["content_sha256"], f["kept_messages"]))
    assert SB.build_subset(_itch_source(itch_gz), SYMS, w, tmp_path / "c").manifest["pinned"] is True


def test_default_pins_are_well_formed() -> None:
    assert len(SB.PINS) == 2
    for (src, syms, tag), (sha, n) in SB.PINS.items():
        assert src in ("nasdaq-2019-12-30", "iex-2026-10-09") and tag == "0935-5m" and len(syms) == 4
        assert len(sha) == 64 and int(sha, 16) >= 0 and n > 100_000


def _padded(itch_bytes: bytes, mb: int) -> bytes:
    """The day plus `mb` MB of late messages of an unrequested locate (all after the cut)."""
    late = F.itch_frame(F._ih("D", 3, F.hms(15, 0)) + struct.pack(">Q", 1))
    return itch_bytes + late * (mb * 1_000_000 // len(late))


def test_download_cap(tmp_path: Path, events: list[F.Ev], itch_bytes: bytes) -> None:
    w = DemoWindow("2019-12-30", 1)
    # no session close and 6 MB of messages stamped before the cut: the cut is never reached
    stuck = F.itch_frame(F._ih("D", 3, F.hms(9, 41)) + struct.pack(">Q", 1))
    p = tmp_path / "stuck.itch50"
    p.write_bytes(F.itch_day(SYMS, events, close=False) + stuck * (6_000_000 // len(stuck)))
    with pytest.raises(QtError) as e:
        SB.build_subset(_itch_source(p), SYMS, w, tmp_path / "c", max_download_bytes=2 * CHUNK)
    assert e.value.code == "QT-INPUT-002" and "without reaching the cut" in e.value.message
    assert not list((tmp_path / "c").iterdir())
    q = tmp_path / "day.itch50"
    q.write_bytes(_padded(itch_bytes, 6))
    r = SB.build_subset(_itch_source(q), SYMS, w, tmp_path / "d", max_download_bytes=8 * CHUNK)
    assert r.manifest["filter"]["cut_reached"]


def test_disk_guard(tmp_path: Path, itch_gz: Path) -> None:
    with pytest.raises(QtError) as e:
        SB.build_subset(
            _itch_source(itch_gz), SYMS, DemoWindow("2019-12-30", 1), tmp_path / "c", min_free_bytes=1 << 62
        )
    assert e.value.code == "QT-INPUT-001" and "free disk space" in e.value.message


@pytest.fixture
def served(tmp_path: Path, itch_bytes: bytes) -> Iterator[tuple[str, int]]:
    """A local HTTP server with a 30 MB day file (`(url, size)`)."""
    root = tmp_path / "www"
    root.mkdir()
    data = _padded(itch_bytes, 30)
    (root / "12302019.NASDAQ_ITCH50").write_bytes(data)

    class H(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a: object) -> None:
            pass

        def copyfile(self, src, dst) -> None:  # type: ignore[no-untyped-def]
            try:
                super().copyfile(src, dst)
            except (BrokenPipeError, ConnectionResetError):  # the demo stops reading at the cut
                pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=str(root)))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/12302019.NASDAQ_ITCH50", len(data)
    srv.shutdown()
    srv.server_close()


def test_http_stream_stops_soon_after_the_cut(tmp_path: Path, served: tuple[str, int], itch_bytes: bytes) -> None:
    url, size = served
    src = custom_source("itch", "2019-12-30", url, tuple(SYMS))
    r = SB.build_subset(src, SYMS, DemoWindow("2019-12-30", 1), tmp_path / "c")
    m = r.manifest
    assert m["source"]["remote"] is True and m["source"]["http_status"] == 200 and m["source"]["total_bytes"] == size
    assert m["filter"]["cut_reached"]
    # the cut lies in the first ~1.3 MB; read-ahead is bounded by the queue (4 chunks) plus the chunks in flight
    assert r.bytes_downloaded <= len(itch_bytes) + 7 * CHUNK < size
    assert (
        gzip.decompress(r.path.read_bytes()) == reference(itch_bytes, SYMS, DemoWindow("2019-12-30", 1).cut_ns_local)[0]
    )


def test_cache_key_distinguishes_inputs(itch_gz: Path) -> None:
    w = DemoWindow("2019-12-30", 5)
    a = SB.cache_key(_itch_source(itch_gz), SYMS, w)
    assert a == SB.cache_key(_itch_source(itch_gz), SYMS, w)
    assert a != SB.cache_key(_itch_source(itch_gz), SYMS, w, "123:456")
    assert a != SB.cache_key(_itch_source(itch_gz), ["AAPL"], w)
    assert a != SB.cache_key(_itch_source(itch_gz), SYMS, DemoWindow("2019-12-30", 6))
    from queuetruth.demo.sources import get_source

    assert SB.cache_key(get_source("nasdaq"), ["AAPL", "MSFT", "SPY", "TSLA"], w) == (
        "nasdaq-2019-12-30_0935-5m_AAPL-MSFT-SPY-TSLA"
    )
