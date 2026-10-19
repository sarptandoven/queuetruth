"""Filesystem store under `STORE_ROOT` for dev and tests. URLs point at `/v1/_store/<key>` and carry
`exp` (unix seconds) and `sig` = HMAC-SHA256(`SIGNING_KEY`, `method|key|part|exp`) (`part` empty for GET);
the API verifies them (`routers/store.py`). Multipart parts are staged under `.multipart/<key>/` until completion,
next to `_meta.json` (declared size and part size): every part must be exactly its expected length (a longer body is
refused while it streams) and completion re-checks the layout (security review M1)."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import secrets
import shutil
import tempfile
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from queuetruth_service.settings import Settings
from queuetruth_service.store.base import (
    NotFoundError,
    PartTooLarge,
    PartUrl,
    SizeMismatch,
    StoreError,
    check_key,
    check_part_sizes,
    expected_part_size,
)

CHUNK = 1 << 20


class LocalStore:
    """`ObjectStore` on the local filesystem."""

    kind = "local"
    endpoint: str | None = None

    def __init__(self, root: Path, signing_key: bytes, public_base_url: str = "", ttl_s: int = 900) -> None:
        self.root = Path(root).resolve()
        self.objects = self.root / "objects"
        self.staging = self.root / ".multipart"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.staging.mkdir(parents=True, exist_ok=True)
        self.signing_key = signing_key
        self.base = public_base_url.rstrip("/")
        self.ttl_s = min(ttl_s, 900)

    @classmethod
    def from_settings(cls, s: Settings) -> LocalStore:
        """Store configured from settings."""
        return cls(Path(s.store_root), s.signing_secret, s.public_base_url, s.presign_ttl_s)

    # ---- signing -------------------------------------------------------------------------------------------

    def signature(self, method: str, key: str, part: str, exp: int) -> str:
        """HMAC of `method|key|part|exp`."""
        msg = f"{method.upper()}|{key}|{part}|{exp}".encode()
        return hmac.new(self.signing_key, msg, hashlib.sha256).hexdigest()

    def verify(self, method: str, key: str, part: str, exp: str | None, sig: str | None) -> bool:
        """True for an unexpired URL signed for exactly this method, key and part."""
        if not exp or not sig or not exp.isdigit() or int(exp) < time.time():
            return False
        return hmac.compare_digest(self.signature(method, key, part, int(exp)), sig)

    def _url(self, method: str, key: str, part: str, ttl_s: int) -> tuple[str, datetime]:
        exp = int(time.time()) + min(ttl_s, self.ttl_s)
        sig = self.signature(method, key, part, exp)
        q = f"exp={exp}&sig={sig}" if not part else f"part={part}&exp={exp}&sig={sig}"
        return f"{self.base}/v1/_store/{quote(key, safe='/=')}?{q}", datetime.fromtimestamp(exp, UTC)

    # ---- paths ---------------------------------------------------------------------------------------------

    def path(self, key: str) -> Path:
        """Filesystem path of an object (inside the store root)."""
        p = (self.objects / check_key(key)).resolve()
        if not p.is_relative_to(self.objects):
            raise StoreError("key escapes the store root")
        return p

    def _stage(self, key: str) -> Path:
        return self.staging / check_key(key)

    # ---- multipart -----------------------------------------------------------------------------------------

    def create_multipart(self, key: str, size_bytes: int, part_size: int) -> tuple[str, list[PartUrl]]:
        """Starts a staged upload and returns its id and part URLs."""
        n = max(1, math.ceil(size_bytes / part_size))
        if n > 10_000:
            raise StoreError("more than 10,000 parts")
        mp_id = secrets.token_hex(16)
        stage = self._stage(key)
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True)
        (stage / "_meta.json").write_text(json.dumps({"size_bytes": int(size_bytes), "part_size": int(part_size)}))
        (stage / "_multipart_id").write_text(mp_id)
        return mp_id, self.part_urls(key, mp_id, n)

    def part_urls(self, key: str, multipart_id: str, n_parts: int) -> list[PartUrl]:
        """Fresh part URLs of a pending upload."""
        urls = []
        for i in range(1, n_parts + 1):
            url, exp = self._url("PUT", key, str(i), self.ttl_s)
            urls.append(PartUrl(i, url, exp))
        return urls

    def _mp_id(self, key: str) -> str | None:
        f = self._stage(key) / "_multipart_id"
        return f.read_text() if f.exists() else None

    def _meta(self, key: str) -> dict:
        try:
            return json.loads((self._stage(key) / "_meta.json").read_text())
        except (OSError, ValueError) as e:
            raise NotFoundError(key) from e

    def part_limit(self, key: str, part: int) -> int:
        """Exact length part `part` of the pending upload at `key` must have."""
        if self._mp_id(key) is None:
            raise NotFoundError(key)
        meta = self._meta(key)
        return expected_part_size(meta["size_bytes"], meta["part_size"], part)

    def put_part(self, key: str, part: int, chunks: Iterable[bytes]) -> str:
        """Stores one part (called by the signed PUT endpoint); returns its quoted md5 ETag. The body must be exactly
        the part's expected length (`PartTooLarge` / `SizeMismatch` otherwise; nothing is kept)."""
        limit = self.part_limit(key, part)
        md5 = hashlib.md5(usedforsecurity=False)
        stage = self._stage(key)
        fd, tmp = tempfile.mkstemp(dir=stage, prefix=".part-")
        n = 0
        try:
            with os.fdopen(fd, "wb") as f:
                for c in chunks:
                    n += len(c)
                    if n > limit:
                        raise PartTooLarge(f"part {part} is longer than {limit} bytes")
                    md5.update(c)
                    f.write(c)
            if n != limit:
                raise SizeMismatch(f"part {part} has {n} bytes, expected exactly {limit}")
        except BaseException:
            os.unlink(tmp)
            raise
        os.replace(tmp, stage / f"part-{part:05d}")
        etag = f'"{md5.hexdigest()}"'
        (stage / f"part-{part:05d}.etag").write_text(etag)
        return etag

    def complete_multipart(
        self, key: str, multipart_id: str, parts: list[dict], size_bytes: int, part_size: int
    ) -> None:
        """Assembles the parts in order; ETags must match the stored parts and the stored sizes the declared layout
        (`SizeMismatch`)."""
        stage = self._stage(key)
        if self._mp_id(key) != multipart_id:
            raise StoreError("unknown multipart upload")
        ordered = sorted(parts, key=lambda p: int(p["part_number"]))
        for p in ordered:
            etag_file = stage / f"part-{int(p['part_number']):05d}.etag"
            if not etag_file.exists() or etag_file.read_text().strip('"') != str(p["etag"]).strip('"'):
                raise StoreError(f"part {p['part_number']}: missing or ETag mismatch")
        sizes = {int(p["part_number"]): (stage / f"part-{int(p['part_number']):05d}").stat().st_size for p in ordered}
        check_part_sizes(size_bytes, part_size, sizes)
        dest = self.path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".assemble-")
        with os.fdopen(fd, "wb") as out:
            for p in ordered:
                with open(stage / f"part-{int(p['part_number']):05d}", "rb") as f:
                    shutil.copyfileobj(f, out, CHUNK)
        os.replace(tmp, dest)
        shutil.rmtree(stage, ignore_errors=True)

    def abort_multipart(self, key: str, multipart_id: str) -> None:
        """Drops staged parts."""
        if self._mp_id(key) == multipart_id:
            shutil.rmtree(self._stage(key), ignore_errors=True)

    def staged_bytes(self, key: str, multipart_id: str) -> int:
        """Bytes held by the staged parts of a pending upload (billed while pending)."""
        stage = self._stage(key)
        if self._mp_id(key) != multipart_id:
            return 0
        return sum(p.stat().st_size for p in stage.glob("part-*") if not p.name.endswith(".etag"))

    # ---- objects -------------------------------------------------------------------------------------------

    def put_file(self, key: str, path: Path) -> str:
        """Copies a local file into the store; returns its sha256."""
        dest = self.path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        h = hashlib.sha256()
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".put-")
        with os.fdopen(fd, "wb") as out, open(path, "rb") as f:
            for chunk in iter(lambda: f.read(CHUNK), b""):
                h.update(chunk)
                out.write(chunk)
        os.replace(tmp, dest)
        return h.hexdigest()

    def get_to_file(self, key: str, path: Path) -> None:
        """Copies an object to a local file."""
        src = self.path(key)
        if not src.is_file():
            raise NotFoundError(key)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, path)

    def presign_get(self, key: str, ttl_s: int = 900) -> str:
        """Signed GET URL for one key."""
        return self._url("GET", key, "", ttl_s)[0]

    def head(self, key: str) -> dict:
        """`{bytes}` of an object."""
        p = self.path(key)
        if not p.is_file():
            raise NotFoundError(key)
        return {"bytes": p.stat().st_size}

    def sha256(self, key: str) -> str:
        """Streaming sha256 of an object."""
        p = self.path(key)
        if not p.is_file():
            raise NotFoundError(key)
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(CHUNK), b""):
                h.update(chunk)
        return h.hexdigest()

    def list_prefix(self, prefix: str) -> list[tuple[str, int]]:
        """`(key, bytes)` of every object under a prefix, sorted by key."""
        base = self.objects / prefix.rstrip("/") if prefix.rstrip("/") else self.objects
        if not base.exists():
            return []
        out = []
        for p in sorted(base.rglob("*")):
            if p.is_file() and not p.name.startswith((".put-", ".assemble-")):
                key = p.relative_to(self.objects).as_posix()
                if key.startswith(prefix):
                    out.append((key, p.stat().st_size))
        return out

    def delete_prefix(self, prefix: str) -> int:
        """Deletes every object (and staged upload) under a prefix; returns the object count."""
        objs = self.list_prefix(prefix)
        for key, _ in objs:
            self.path(key).unlink(missing_ok=True)
        for base in (self.objects, self.staging):
            d = base / prefix.rstrip("/")
            if prefix.rstrip("/") and d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
        return len(objs)

    def check(self) -> bool:
        """True when the root is writable."""
        return os.access(self.objects, os.W_OK)
