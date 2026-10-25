"""`queuetruth.demo.fetch`: file and HTTP byte streams (with ranged resumption after a dropped connection) and the
gzip / raw inflater. The HTTP server is a local `http.server` thread; nothing leaves the machine."""

from __future__ import annotations

import gzip
import hashlib
import http.server
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from queuetruth.demo.fetch import USER_AGENT, Inflater, Stream
from queuetruth.errors import QtError

DATA = bytes(range(256)) * 20_000  # 5.12 MB


class _Handler(http.server.BaseHTTPRequestHandler):
    """Serves `DATA` at /data with byte ranges; behaviour switches on the server object."""

    def log_message(self, *a: object) -> None:  # quiet
        pass

    def do_GET(self) -> None:
        try:
            self._get()
        except (BrokenPipeError, ConnectionResetError):  # the client stopped reading (expected in some tests)
            pass

    def _get(self) -> None:
        srv = self.server
        srv.requests.append((self.path, self.headers.get("Range"), self.headers.get("User-Agent")))  # type: ignore[attr-defined]
        srv.if_range.append(self.headers.get("If-Range"))  # type: ignore[attr-defined]
        if self.path != "/data":
            self.send_error(404, "not here")
            return
        rng = self.headers.get("Range")
        start = 0
        if rng:
            if not srv.ranges:  # type: ignore[attr-defined]
                self.send_response(200)
                self.send_header("Content-Length", str(len(DATA)))
                self.end_headers()
                self.wfile.write(DATA)
                return
            start = int(rng.split("=")[1].split("-")[0])
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(DATA) - 1}/{len(DATA)}")
        else:
            self.send_response(200)
        body = DATA[start:]
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", srv.etag)  # type: ignore[attr-defined]
        self.end_headers()
        drop = srv.drop_after  # type: ignore[attr-defined]
        if drop and not rng:
            self.wfile.write(body[:drop])
            self.wfile.flush()
            self.connection.shutdown(2)  # the client sees a short read
            srv.etag = srv.etag_after_drop or srv.etag  # type: ignore[attr-defined]
            return
        self.wfile.write(body)


@pytest.fixture
def server() -> Iterator[http.server.ThreadingHTTPServer]:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.requests = []  # type: ignore[attr-defined]
    srv.ranges = True  # type: ignore[attr-defined]
    srv.drop_after = 0  # type: ignore[attr-defined]
    srv.if_range = []  # type: ignore[attr-defined]
    srv.etag = '"x1"'  # type: ignore[attr-defined]
    srv.etag_after_drop = None  # type: ignore[attr-defined]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _url(srv: http.server.ThreadingHTTPServer, path: str = "/data") -> str:
    return f"http://127.0.0.1:{srv.server_address[1]}{path}"


def test_file_stream_counts_and_hashes(tmp_path: Path) -> None:
    p = tmp_path / "f.bin"
    p.write_bytes(DATA)
    for loc in (str(p), f"file://{p}"):
        s = Stream(loc)
        got = b"".join(s.chunks())
        assert got == DATA
        assert s.bytes_read == len(DATA) and s.total_bytes == len(DATA)
        assert s.sha256.hexdigest() == hashlib.sha256(DATA).hexdigest()


def test_file_stream_missing_file(tmp_path: Path) -> None:
    with pytest.raises(QtError) as e:
        list(Stream(str(tmp_path / "nope.gz")).chunks())
    assert e.value.code == "QT-INPUT-001"


def test_file_stream_close_stops_early(tmp_path: Path) -> None:
    p = tmp_path / "f.bin"
    p.write_bytes(DATA)
    s = Stream(str(p))
    it = s.chunks()
    first = next(it)
    s.close()
    assert list(it) == []
    assert s.bytes_read == len(first) < len(DATA)


def test_http_stream_full(server: http.server.ThreadingHTTPServer) -> None:
    s = Stream(_url(server))
    assert b"".join(s.chunks()) == DATA
    assert s.http_status == 200 and s.total_bytes == len(DATA) and s.resumptions == 0
    assert s.headers["etag"] == '"x1"'
    assert server.requests[0][2] == USER_AGENT  # type: ignore[attr-defined]


def test_http_stream_resumes_after_a_dropped_connection(server: http.server.ThreadingHTTPServer) -> None:
    server.drop_after = 1_000_003  # type: ignore[attr-defined]
    s = Stream(_url(server), backoff_s=0.0)
    got = b"".join(s.chunks())
    assert got == DATA
    assert s.resumptions >= 1
    ranges = [r for _, r, _ in server.requests if r]  # type: ignore[attr-defined]
    assert ranges and ranges[0].startswith("bytes=") and int(ranges[0][6:].split("-")[0]) > 0
    assert s.sha256.hexdigest() == hashlib.sha256(DATA).hexdigest()
    assert server.if_range[0] is None and set(server.if_range[1:]) == {'"x1"'}  # type: ignore[attr-defined]


def test_http_resume_refuses_a_file_that_changed(server: http.server.ThreadingHTTPServer) -> None:
    server.drop_after = 1_000_003  # type: ignore[attr-defined]
    server.etag_after_drop = '"x2"'  # type: ignore[attr-defined]
    s = Stream(_url(server), backoff_s=0.0)
    with pytest.raises(QtError) as e:
        b"".join(s.chunks())
    assert e.value.code == "QT-INPUT-001" and "the file changed" in e.value.message
    assert s.bytes_read == 1_000_003  # nothing of the new version was taken


def test_tls_certificate_failure_is_not_retried() -> None:
    import ssl
    import urllib.error

    calls: list[object] = []

    def opener(req: object, timeout: float) -> object:
        calls.append(req)
        raise urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed"))

    s = Stream("https://example.invalid/x.gz", opener=opener, backoff_s=0.0)
    with pytest.raises(QtError) as e:
        list(s.chunks())
    assert e.value.code == "QT-INPUT-001" and "certificate" in e.value.message
    assert "Install Certificates" in (e.value.hint or "") and len(calls) == 1


def test_inflater_bounds_each_piece() -> None:
    from queuetruth.demo.fetch import MAX_PIECE

    raw = bytes(3 * MAX_PIECE + 12345)  # zeros: about 1000:1
    z = gzip.compress(raw, compresslevel=9, mtime=0)
    inf = Inflater()
    sizes = [len(p) for p in inf.pieces(z)]
    assert max(sizes) <= MAX_PIECE and len(sizes) >= 4
    tail = inf.flush()
    assert sum(sizes) + len(tail) == len(raw) == inf.bytes_out


def test_http_resume_without_range_support_is_an_error(server: http.server.ThreadingHTTPServer) -> None:
    server.drop_after = 500_000  # type: ignore[attr-defined]
    server.ranges = False  # type: ignore[attr-defined]
    s = Stream(_url(server), backoff_s=0.0)
    with pytest.raises(QtError) as e:
        b"".join(s.chunks())
    assert e.value.code == "QT-INPUT-001"
    assert "not 206" in e.value.message


def test_http_404(server: http.server.ThreadingHTTPServer) -> None:
    with pytest.raises(QtError) as e:
        list(Stream(_url(server, "/missing")).chunks())
    assert e.value.code == "QT-INPUT-001" and "404" in e.value.message


def test_http_unreachable_gives_up_after_retries() -> None:
    s = Stream("http://127.0.0.1:9/never", retries=2, backoff_s=0.0, timeout_s=2)
    with pytest.raises(QtError) as e:
        list(s.chunks())
    assert e.value.code == "QT-INPUT-001"


def _feed_all(inf: Inflater, data: bytes, step: int) -> bytes:
    out = bytearray()
    for i in range(0, len(data), step):
        out += inf.feed(data[i : i + step])
    return bytes(out + inf.flush())


@pytest.mark.parametrize("step", [1, 7, 4096, 1 << 20])
def test_inflater_multi_member_gzip(step: int) -> None:
    raw = DATA[:300_000]
    z = gzip.compress(raw[:100_000], mtime=0) + gzip.compress(raw[100_000:], mtime=0)
    inf = Inflater()
    assert _feed_all(inf, z, step) == raw
    assert inf.mode == "gzip" and inf.members == 2 and inf.bytes_out == len(raw)


@pytest.mark.parametrize("data", [b"", b"x", b"\x00\x0cS" * 1000])
def test_inflater_raw_passthrough(data: bytes) -> None:
    inf = Inflater()
    assert _feed_all(inf, data, 5) == data
    assert inf.mode == "raw" and inf.bytes_out == len(data)


def test_inflater_corrupt_gzip() -> None:
    z = bytearray(gzip.compress(DATA[:100_000], mtime=0))
    z[20:40] = b"\xff" * 20
    with pytest.raises(QtError) as e:
        _feed_all(Inflater(), bytes(z), 1 << 16)
    assert e.value.code == "QT-INPUT-002"


def test_inflater_truncated_gzip_yields_the_prefix() -> None:
    z = gzip.compress(DATA, mtime=0)
    out = _feed_all(Inflater(), z[: len(z) // 2], 1 << 14)
    assert 0 < len(out) < len(DATA) and DATA.startswith(out)
