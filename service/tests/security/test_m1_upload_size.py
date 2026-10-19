"""M1 (security review, inverted): the declared `size_bytes` is enforced by the store, not trusted. Local-store parts
are capped at their exact expected length while streaming; completion checks every part against the declared layout
(S3: `ListParts`) and a mismatch fails the upload and deletes its bytes; verification fails a size mismatch from
`head` alone (no hashing) and deletes the object; the storage meter bills measured bytes; abandoned pending uploads
expire."""

from __future__ import annotations

import asyncio
import hashlib
import os
from urllib.parse import urlsplit

import httpx
import pytest
from svc_support import upload_bytes

from queuetruth_service.db.pool import make_async_pool
from queuetruth_service.maintenance import expire_pending_uploads, storage_meter, verify_uploads
from queuetruth_service.store import NotFoundError, SizeMismatch, upload_prefix
from queuetruth_service.store.s3 import S3Store, s3_client


class CountingStore:
    """Delegates to a store and records the calls the verification pass makes."""

    def __init__(self, inner):
        self.inner = inner
        self.calls: list[str] = []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def head(self, key):
        self.calls.append("head")
        return self.inner.head(key)

    def sha256(self, key):
        self.calls.append("sha256")
        return self.inner.sha256(key)


def _create(svc, size: int, sha: str, key: str) -> dict:
    body = {"purpose": "other", "filename": "f.bin", "size_bytes": size, "sha256": sha}
    r = svc.client.post("/v1/uploads", json=body, headers=svc.h(Idempotency_Key=key))
    assert r.status_code == 201, r.text
    return r.json()


def _put(svc, url: str, data: bytes):
    u = urlsplit(url)
    return svc.client.put(f"{u.path}?{u.query}", content=data)


async def _with_worker_pool(svc, fn):
    pool = await make_async_pool(svc.db.worker)
    try:
        return await fn(pool)
    finally:
        await pool.close()


@pytest.mark.requires_postgres
def test_local_store_caps_each_part_at_its_exact_length(svc):
    declared = b"x"
    up = _create(svc, 1, hashlib.sha256(declared).hexdigest(), "big-1")
    url = up["parts"][0]["url"]
    r = _put(svc, url, os.urandom(512 * 1024))  # 512x the declared size, 512x the part size (1024)
    assert r.status_code == 413
    r = _put(svc, url, os.urandom(1000))  # within the part size, still longer than this (only) part
    assert r.status_code == 413
    r = _put(svc, url, b"")
    assert r.status_code == 422 and r.json()["type"].endswith("/part-size-mismatch")
    r = _put(svc, url, declared)
    assert r.status_code == 200
    done = svc.client.post(
        f"/v1/uploads/{up['id']}/complete",
        json={"parts": [{"part_number": 1, "etag": r.headers["etag"]}]},
        headers=svc.h(Idempotency_Key="big-1c"),
    )
    assert done.status_code == 200 and done.json()["status"] == "complete"
    key = svc.db.q("select object_key from uploads where id = %s", (up["id"],))[0][0]
    assert svc.store.path(key).stat().st_size == 1
    # multipart: non-last parts must be exactly the part size
    data = os.urandom(2048 + 10)
    up2 = _create(svc, len(data), hashlib.sha256(data).hexdigest(), "mp-1")
    assert len(up2["parts"]) == 3
    assert _put(svc, up2["parts"][0]["url"], data[:1000]).status_code == 422
    assert _put(svc, up2["parts"][2]["url"], data[2048:] + b"extra").status_code == 413


@pytest.mark.requires_postgres
def test_completion_rejects_a_size_mismatch_fails_the_upload_and_deletes_its_parts(svc):
    data = b"y" * 100
    up = _create(svc, len(data), hashlib.sha256(data).hexdigest(), "mm-1")
    key, mp = svc.db.q("select object_key, multipart_id from uploads where id = %s", (up["id"],))[0]
    stage = svc.store._stage(key)
    (stage / "part-00001").write_bytes(b"y" * 5000)  # bytes that bypassed the PUT endpoint
    (stage / "part-00001.etag").write_text('"e"')
    r = svc.client.post(
        f"/v1/uploads/{up['id']}/complete",
        json={"parts": [{"part_number": 1, "etag": "e"}]},
        headers=svc.h(Idempotency_Key="mm-1c"),
    )
    assert r.status_code == 422 and r.headers["content-type"] == "application/problem+json"
    assert r.json()["type"].endswith("/upload-size-mismatch")
    assert svc.db.q("select status, stored_bytes from uploads where id = %s", (up["id"],)) == [("failed", 0)]
    assert not stage.exists() and not svc.store.path(key).exists()
    assert svc.store.staged_bytes(key, mp) == 0
    replay = svc.client.post(
        f"/v1/uploads/{up['id']}/complete",
        json={"parts": [{"part_number": 1, "etag": "e"}]},
        headers=svc.h(Idempotency_Key="mm-1c"),
    )
    assert replay.status_code == 422 and replay.headers.get("idempotent-replayed") == "true"


@pytest.mark.requires_postgres
def test_verification_fails_size_mismatch_without_hashing_and_deletes_the_object(svc):
    up = upload_bytes(svc, b"z" * 1500)
    key = svc.db.q("select object_key from uploads where id = %s", (up,))[0][0]
    svc.store.path(key).write_bytes(os.urandom(512 * 1024))  # the stored object is not what was declared
    counting = CountingStore(svc.store)
    verified = asyncio.run(_with_worker_pool(svc, lambda pool: verify_uploads(pool, counting)))
    assert verified == {up: "failed"}
    assert counting.calls == ["head"]  # decided from the size alone
    assert not svc.store.path(key).exists()
    assert svc.store.list_prefix(upload_prefix(svc.tenants["A"], up)) == []
    good = upload_bytes(svc, b"g" * 2048, filename="g.bin")
    counting.calls.clear()
    assert asyncio.run(_with_worker_pool(svc, lambda pool: verify_uploads(pool, counting))) == {good: "verified"}
    assert counting.calls == ["head", "sha256"]
    assert svc.db.q("select stored_bytes from uploads where id = %s", (good,)) == [(2048,)]


@pytest.mark.requires_postgres
def test_storage_meter_bills_measured_bytes_not_declared(svc):
    good = upload_bytes(svc, b"g" * 2048, filename="g.bin")
    assert asyncio.run(_with_worker_pool(svc, lambda pool: verify_uploads(pool, svc.store))) == {good: "verified"}
    key = svc.db.q("select object_key from uploads where id = %s", (good,))[0][0]
    svc.db.q("update uploads set size_bytes = 1 where id = %s", (good,))  # a declaration that understates
    pending = _create(svc, 3000, "0" * 64, "pend-1")
    assert _put(svc, pending["parts"][0]["url"], b"p" * 1024).status_code == 200  # 1 KiB staged, not complete
    meter = asyncio.run(_with_worker_pool(svc, lambda pool: storage_meter(pool, svc.store, "2026-10-18")))
    assert svc.store.path(key).stat().st_size == 2048
    assert meter[svc.tenants["A"]] == 2048.0 + 1024.0  # measured objects plus staged parts
    assert meter[svc.tenants["B"]] == 0.0


@pytest.mark.requires_postgres
def test_abandoned_pending_uploads_expire_and_lose_their_parts(svc):
    up = _create(svc, 3000, "0" * 64, "old-1")
    assert _put(svc, up["parts"][0]["url"], b"o" * 1024).status_code == 200
    key, mp = svc.db.q("select object_key, multipart_id from uploads where id = %s", (up["id"],))[0]
    assert svc.store.staged_bytes(key, mp) == 1024
    svc.db.q("update uploads set created_at = now() - interval '2 days' where id = %s", (up["id"],))
    expired = asyncio.run(_with_worker_pool(svc, lambda pool: expire_pending_uploads(pool, svc.store, 86_400)))
    assert expired == [up["id"]]
    assert svc.db.q("select status from uploads where id = %s", (up["id"],)) == [("failed",)]
    assert svc.store.staged_bytes(key, mp) == 0


def test_s3_completion_verifies_part_sizes_with_list_parts(moto_endpoint):
    c = s3_client(moto_endpoint, "us-east-1", "testing", "testing")
    c.create_bucket(Bucket="qtsec")
    store = S3Store("qtsec", c)
    part = 5 * 1024 * 1024
    key = "t/ten_A/uploads/up_1/f.bin"
    mp_id, urls = store.create_multipart(key, size_bytes=1, part_size=part)
    big = os.urandom(6 * 1024 * 1024)  # > declared size and > part size
    r = httpx.put(urls[0].url, content=big, timeout=60)
    assert r.status_code == 200  # presigned UploadPart URLs cannot bind the length ...
    assert store.staged_bytes(key, mp_id) == len(big)  # ... (billed while pending) ...
    with pytest.raises(SizeMismatch):  # ... so completion refuses the layout
        store.complete_multipart(key, mp_id, [{"part_number": 1, "etag": r.headers["ETag"]}], 1, part)
    with pytest.raises(NotFoundError):
        store.head(key)
    store.abort_multipart(key, mp_id)
    assert c.list_multipart_uploads(Bucket="qtsec").get("Uploads", []) == []
    # the honest layout completes: 5 MiB + 1 MiB
    data = os.urandom(part + 1024 * 1024)
    key2 = "t/ten_A/uploads/up_2/f.bin"
    mp2, urls2 = store.create_multipart(key2, size_bytes=len(data), part_size=part)
    parts = []
    for i, u in enumerate(urls2):
        rr = httpx.put(u.url, content=data[i * part : (i + 1) * part], timeout=60)
        parts.append({"part_number": i + 1, "etag": rr.headers["ETag"]})
    store.complete_multipart(key2, mp2, parts, len(data), part)
    assert store.head(key2)["bytes"] == len(data)
