"""Byte sources for `queuetruth demo`: an HTTP(S) stream that resumes with a byte range after a dropped connection,
or a local file; the bytes that passed through are counted and hashed (sha256), and gzip input is inflated (every
member of a multi-member file) while anything else passes through unchanged.

`Stream(location)`: `https://...`, `http://...`, `file:///...` or a plain path. `chunks()` yields the raw
(compressed) bytes; `close()` stops early (the demo reads only a prefix of a day file). A resumption sends
`Range: bytes=<offset>-` plus `If-Range` with the first response's strong `ETag` (else its `Last-Modified`), and
requires HTTP 206 with a `Content-Range` that starts at the offset and the same `ETag`; anything else (a 200 because
the object changed, another validator) is `QT-INPUT-001`, never a silent restart from byte 0 or a splice of two
versions of the file. Network errors are retried with backoff, except a failed TLS certificate check (retrying
cannot fix it).

`Inflater`: detects gzip by its magic bytes (1f 8b) on the first chunk; a zlib error is `QT-INPUT-002`. `pieces()`
yields at most `MAX_PIECE` decompressed bytes at a time, so a hostile or broken input that inflates enormously
cannot hold more than that per call in memory.
"""

from __future__ import annotations

import hashlib
import http.client
import re
import ssl
import time
import urllib.error
import urllib.request
import zlib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from queuetruth._version import __version__
from queuetruth.errors import QtError

CHUNK = 1 << 20
MAX_PIECE = 8 << 20
USER_AGENT = f"queuetruth/{__version__} (demo; +https://github.com/sarptandoven/queuetruth)"
_RETRYABLE = (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError, OSError)


def is_url(location: str) -> bool:
    return location.startswith(("http://", "https://"))


def local_path(location: str) -> Path:
    return Path(location[7:]) if location.startswith("file://") else Path(location)


class Stream:
    """Raw bytes of one location (module docstring)."""

    def __init__(
        self,
        location: str,
        *,
        retries: int = 5,
        timeout_s: float = 60.0,
        backoff_s: float = 1.0,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.location = location
        self.retries = retries
        self.timeout_s = timeout_s
        self.backoff_s = backoff_s
        self._open = opener or urllib.request.urlopen
        self.bytes_read = 0
        self.sha256 = hashlib.sha256()
        self.resumptions = 0
        self.total_bytes: int | None = None
        self.http_status: int | None = None
        self.headers: dict[str, str] = {}
        self._resp: Any = None
        self._file: Any = None
        self._closed = False
        self._validator: tuple[str, str] | None = None  # ("etag" | "last-modified", value) of the first response

    # ---- HTTP
    def _request(self, offset: int) -> Any:
        req = urllib.request.Request(self.location, headers={"User-Agent": USER_AGENT})
        if offset:
            req.add_header("Range", f"bytes={offset}-")
            if self._validator is not None:
                req.add_header("If-Range", self._validator[1])
        try:
            resp = self._open(req, timeout=self.timeout_s)
        except urllib.error.HTTPError as e:
            raise QtError(
                "QT-INPUT-001",
                f"GET {self.location}: HTTP {e.code} {e.reason}",
                "check the URL; the demo needs a server that answers byte-range requests for resumption",
            ) from None
        except urllib.error.URLError as e:
            if isinstance(e.reason, ssl.SSLCertVerificationError):
                raise _cert_error(self.location, e.reason) from None
            raise
        except ssl.SSLCertVerificationError as e:
            raise _cert_error(self.location, e) from None
        status = getattr(resp, "status", None) or resp.getcode()
        if offset:
            cr = resp.headers.get("Content-Range", "")
            m = re.match(r"^bytes (\d+)-", cr)
            if status != 206 or not m or int(m.group(1)) != offset:
                resp.close()
                raise QtError(
                    "QT-INPUT-001",
                    f"GET {self.location}: resuming at byte {offset} got HTTP {status} ({cr or 'no Content-Range'}), "
                    "not 206 from that byte (the server does not answer byte ranges, or the file changed)",
                    "rerun the demo",
                )
            etag = resp.headers.get("ETag")
            if self._validator is not None and self._validator[0] == "etag" and etag != self._validator[1]:
                resp.close()
                raise QtError(
                    "QT-INPUT-001",
                    f"GET {self.location}: the file changed while it was read (ETag {self._validator[1]} at the "
                    f"start, {etag or 'none'} when resuming at byte {offset})",
                    "rerun the demo",
                )
        else:
            self.http_status = status
            for k in ("Content-Length", "ETag", "Last-Modified", "Content-Type"):
                v = resp.headers.get(k)
                if v is not None:
                    self.headers[k.lower()] = v
            cl = resp.headers.get("Content-Length")
            if cl and cl.isdigit():
                self.total_bytes = int(cl)
            etag, lm = self.headers.get("etag"), self.headers.get("last-modified")
            if etag and not etag.startswith("W/"):  # If-Range needs a strong validator
                self._validator = ("etag", etag)
            elif lm:
                self._validator = ("last-modified", lm)
        return resp

    def _http_chunks(self) -> Iterator[bytes]:
        attempt = 0
        while True:
            try:
                if self._resp is None:
                    self._resp = self._request(self.bytes_read)
                b = self._resp.read(CHUNK)
            except QtError:
                raise
            except ssl.SSLCertVerificationError as e:
                raise _cert_error(self.location, e) from None
            except _RETRYABLE as e:
                if self._closed:
                    return
                self._drop()
                if attempt >= self.retries:
                    raise QtError(
                        "QT-INPUT-001",
                        f"GET {self.location}: {type(e).__name__}: {e} at byte {self.bytes_read} after "
                        f"{self.resumptions} resumption(s)",
                        "check the network connection and rerun the demo",
                    ) from None
                attempt += 1
                self.resumptions += 1 if self.bytes_read else 0
                time.sleep(min(30.0, self.backoff_s * 2 ** (attempt - 1)))
                continue
            if not b:
                if self.total_bytes is not None and self.bytes_read < self.total_bytes and not self._closed:
                    # a server that closed before Content-Length: resume from where it stopped
                    self._drop()
                    if attempt >= self.retries:
                        raise QtError(
                            "QT-INPUT-001",
                            f"GET {self.location}: connection closed at byte {self.bytes_read} of {self.total_bytes}",
                            "check the network connection and rerun the demo",
                        )
                    attempt += 1
                    self.resumptions += 1
                    time.sleep(min(30.0, self.backoff_s * 2 ** (attempt - 1)))
                    continue
                return
            attempt = 0
            self._take(b)
            yield b

    def _drop(self) -> None:
        if self._resp is not None:
            try:
                self._resp.close()
            except OSError:
                pass
            self._resp = None

    # ---- file
    def _file_chunks(self) -> Iterator[bytes]:
        p = local_path(self.location)
        try:
            self._file = open(p, "rb")
            self.total_bytes = p.stat().st_size
        except OSError as e:
            raise QtError("QT-INPUT-001", f"{p}: {e.strerror or e}", "check the --from-file path") from None
        while not self._closed:
            b = self._file.read(CHUNK)
            if not b:
                return
            self._take(b)
            yield b

    def _take(self, b: bytes) -> None:
        self.bytes_read += len(b)
        self.sha256.update(b)

    def chunks(self) -> Iterator[bytes]:
        try:
            yield from (self._http_chunks() if is_url(self.location) else self._file_chunks())
        finally:
            self.close()

    def close(self) -> None:
        self._closed = True
        self._drop()
        if self._file is not None:
            self._file.close()
            self._file = None


def _cert_error(location: str, e: BaseException) -> QtError:
    return QtError(
        "QT-INPUT-001",
        f"GET {location}: TLS certificate check failed ({e})",
        "install the CA certificates for this Python (python.org builds on macOS: run 'Install Certificates.command' "
        "in the Python folder), or use --from-file with a file you downloaded",
    )


class Inflater:
    """gzip (multi-member) or pass-through, decided on the first bytes (module docstring)."""

    def __init__(self) -> None:
        self.mode: str | None = None
        self._d: Any = None
        self._head = b""
        self.bytes_out = 0
        self.members = 0

    def pieces(self, b: bytes) -> Iterator[bytes]:
        """The decompressed bytes of `b`, at most `MAX_PIECE` per piece (no empty pieces)."""
        if self.mode is None:
            self._head += b
            if len(self._head) < 2:
                return
            self.mode = "gzip" if self._head[:2] == b"\x1f\x8b" else "raw"
            b, self._head = self._head, b""
            if self.mode == "gzip":
                self._d = zlib.decompressobj(31)
        if self.mode == "raw":
            if b:
                self.bytes_out += len(b)
                yield b
            return
        try:
            while b:
                out = self._d.decompress(b, MAX_PIECE)
                if out:
                    self.bytes_out += len(out)
                    yield out
                if self._d.eof:
                    self.members += 1
                    b = self._d.unused_data
                    self._d = zlib.decompressobj(31)
                else:
                    b = self._d.unconsumed_tail
        except zlib.error as e:
            raise QtError("QT-INPUT-002", f"gzip stream: {e} after {self.bytes_out} decompressed bytes") from None

    def feed(self, b: bytes) -> bytes:
        """All decompressed bytes of `b` at once (`pieces` joined)."""
        return b"".join(self.pieces(b))

    def flush(self) -> bytes:
        if self.mode is None:  # fewer than two bytes in total
            self.mode = "raw"
            b, self._head = self._head, b""
            self.bytes_out += len(b)
            return b
        if self.mode == "raw":
            return b""
        tail = self._d.flush()
        self.bytes_out += len(tail)
        return tail


__all__ = ["CHUNK", "MAX_PIECE", "USER_AGENT", "Inflater", "Stream", "is_url", "local_path"]
