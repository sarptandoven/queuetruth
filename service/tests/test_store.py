"""Object stores (SPEC 8.4, O-STORE-1): multipart presigned upload, completion, sha256 verification and ranged
download through the API on `LocalStore` and on `S3Store` against a moto server; HMAC URL signing and scoping."""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from svc_support import insert_job, make_svc, succeed_job, upload_bytes, verify_uploads

from queuetruth_service.store.local import LocalStore
from queuetruth_service.store.s3 import S3Store, s3_client

PART = 5 * 1024 * 1024


def test_local_signing_scoped_and_expiring(tmp_path):
    s = LocalStore(tmp_path, b"k", "http://api.example")
    url = s.presign_get("t/ten_A/jobs/job_1/a.bin", 60)
    q = parse_qs(urlsplit(url).query)
    assert url.startswith("http://api.example/v1/_store/t/ten_A/jobs/job_1/a.bin?")
    exp, sig = q["exp"][0], q["sig"][0]
    assert int(exp) - time.time() <= 60
    assert s.verify("GET", "t/ten_A/jobs/job_1/a.bin", "", exp, sig)
    assert not s.verify("GET", "t/ten_A/jobs/job_1/b.bin", "", exp, sig)  # other key
    assert not s.verify("PUT", "t/ten_A/jobs/job_1/a.bin", "", exp, sig)  # other method
    assert not s.verify("GET", "t/ten_A/jobs/job_1/a.bin", "", str(int(exp) + 1), sig)  # tampered expiry
    old = str(int(time.time()) - 1)
    assert not s.verify(
        "GET", "t/ten_A/jobs/job_1/a.bin", "", old, s.signature("GET", "t/ten_A/jobs/job_1/a.bin", "", int(old))
    )
    assert not LocalStore(tmp_path, b"other").verify("GET", "t/ten_A/jobs/job_1/a.bin", "", exp, sig)
    assert "Expires" not in url and int(exp) - time.time() <= 900
    big = LocalStore(tmp_path, b"k", ttl_s=5000)
    assert big.ttl_s == 900


def test_local_verify_rejects_non_ascii_expiry(tmp_path):
    """Regression (schemathesis, 2026-10-23): `"²".isdigit()` is True but `int("²")` raises, which turned a
    tampered `exp` into a 500. Any expiry that is not ASCII decimal digits is simply not a valid signature."""
    s = LocalStore(tmp_path, b"k", "http://api.example")
    for bad in ("²", "١٢٣", "12³", " 12", "+12", "1_000"):
        assert not s.verify("PUT", "7", "", bad, "0" * 64), bad


def test_local_store_rejects_traversal(tmp_path):
    from queuetruth_service.store import StoreError, safe_filename

    s = LocalStore(tmp_path, b"k")
    for bad in ("../x", "t/../../x", "/abs", "t//x", "t/./x"):
        with pytest.raises(StoreError):
            s.path(bad)
    assert safe_filename("../../etc/passwd") == "passwd" and safe_filename("a b;c.txt") == "a_b_c.txt"


@pytest.mark.requires_postgres
def test_o_store_1_local_multipart_verify_ranged_download(svc):
    data = os.urandom(3 * 1024 + 17)  # part size 1024 in tests -> 4 parts
    up = upload_bytes(svc, data, filename="fills.parquet")
    r = svc.client.get(f"/v1/uploads/{up}", headers=svc.h())
    assert r.json()["status"] == "complete" and r.json()["parts"] == []
    assert verify_uploads(svc) == {up: "verified"}
    assert svc.client.get(f"/v1/uploads/{up}", headers=svc.h()).json()["status"] == "verified"
    # a wrong declared sha256 fails verification
    bad = upload_bytes(svc, b"abc" * 100, filename="x.bin")
    svc.db.q("update uploads set sha256 = %s where id = %s", ("0" * 64, bad))
    assert verify_uploads(svc) == {bad: "failed"}
    # tampered part URL -> 403; part URL reused for another key -> 403
    body = {
        "purpose": "other",
        "filename": "y.bin",
        "size_bytes": 10,
        "sha256": hashlib.sha256(b"0123456789").hexdigest(),
    }
    p = svc.client.post("/v1/uploads", json=body, headers=svc.h(Idempotency_Key="y")).json()["parts"][0]
    u = urlsplit(p["url"])
    assert svc.client.put(f"{u.path}?{u.query.replace('sig=', 'sig=0')}", content=b"0123456789").status_code == 403
    assert svc.client.put(f"{u.path}x?{u.query}", content=b"0123456789").status_code == 403
    # completion with a wrong ETag -> 422, not stored
    assert svc.client.put(f"{u.path}?{u.query}", content=b"0123456789").status_code == 200
    r = svc.client.post(
        f"/v1/uploads/{p_id(svc, 'y.bin')}/complete",
        json={"parts": [{"part_number": 1, "etag": '"deadbeef"'}]},
        headers=svc.h(Idempotency_Key="yc"),
    )
    assert r.status_code == 422
    # ranged download of a job output on the local store (direct stream with Range)
    job = insert_job(svc)
    succeed_job(svc, job, {"dir/out.bin": data})
    man = svc.client.get(f"/v1/jobs/{job}/files", headers=svc.h()).json()
    f = man["files"][0]
    assert f["name"] == "dir/out.bin" and f["sha256"] == hashlib.sha256(data).hexdigest() and f["bytes"] == len(data)
    full = svc.client.get(f"/v1/jobs/{job}/files/dir/out.bin", headers=svc.h())
    assert full.status_code == 200 and full.content == data and full.headers["accept-ranges"] == "bytes"
    part = svc.client.get(f"/v1/jobs/{job}/files/dir/out.bin", headers=svc.h() | {"Range": "bytes=100-"})
    assert part.status_code == 206 and part.content == data[100:]
    assert part.headers["content-range"] == f"bytes 100-{len(data) - 1}/{len(data)}"
    assert (
        svc.client.get(f"/v1/jobs/{job}/files/dir/out.bin", headers=svc.h() | {"Range": "bytes=-10"}).content
        == data[-10:]
    )
    r416 = svc.client.get(f"/v1/jobs/{job}/files/dir/out.bin", headers=svc.h() | {"Range": f"bytes={len(data)}-"})
    assert r416.status_code == 416 and r416.headers["content-type"] == "application/problem+json"
    su = urlsplit(f["url"])
    signed = svc.client.get(f"{su.path}?{su.query}", headers={"Range": "bytes=5-9"})
    assert signed.status_code == 206 and signed.content == data[5:10]
    # the presigned URL needs no API key but expires
    assert int(parse_qs(su.query)["exp"][0]) - time.time() <= 900


def p_id(svc, filename: str) -> str:
    return svc.db.q("select id from uploads where filename = %s", (filename,))[0][0]


@pytest.fixture
def s3svc(db, tmp_path, moto_endpoint):
    client = s3_client(moto_endpoint, "us-east-1", "testing", "testing")
    bucket = "qt-" + os.urandom(4).hex()
    client.create_bucket(Bucket=bucket)
    store = S3Store(bucket, client)
    gen = make_svc(db, tmp_path, part_size_bytes=PART, max_upload_bytes=1 << 30)
    svc = next(gen)
    svc.client.app.state.ctx.store = store
    svc.store = store
    yield svc
    gen.close()


@pytest.mark.requires_postgres
def test_o_store_1_s3_multipart_verify_ranged_download(s3svc, moto_endpoint):
    svc = s3svc
    data = os.urandom(2 * PART + 123_457)
    body = {
        "purpose": "market_data",
        "filename": "d.dbn.zst",
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    r = svc.client.post("/v1/uploads", json=body, headers=svc.h(Idempotency_Key="s3"))
    assert r.status_code == 201, r.text
    up = r.json()
    assert [p["part_number"] for p in up["parts"]] == [1, 2, 3]
    for p in up["parts"]:  # moto cannot verify signatures; assert the URL is scoped to one key and <= 900 s
        assert f"/{svc.store.bucket}/t/{svc.tenants['A']}/uploads/{up['id']}/d.dbn.zst?" in p["url"]
        assert "X-Amz-Expires=900" in p["url"] and "X-Amz-SignedHeaders=host" in p["url"]
    etags = []
    with httpx.Client(timeout=30) as http:  # a plain HTTP client, as customers use
        for p in up["parts"]:
            chunk = data[(p["part_number"] - 1) * PART : p["part_number"] * PART]
            rr = http.put(p["url"], content=chunk)
            assert rr.status_code == 200, rr.text
            etags.append({"part_number": p["part_number"], "etag": rr.headers["ETag"]})
    r = svc.client.post(f"/v1/uploads/{up['id']}/complete", json={"parts": etags}, headers=svc.h(Idempotency_Key="s3c"))
    assert r.status_code == 200 and r.json()["status"] == "complete", r.text
    assert verify_uploads(svc) == {up["id"]: "verified"}
    # job output on S3: 302 to a presigned URL, Range resume on that URL
    job = insert_job(svc)
    out = Path(svc.settings.store_root) / "o.bin"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data[:5000])
    key = f"t/{svc.tenants['A']}/jobs/{job}/o.bin"
    assert svc.store.put_file(key, out) == hashlib.sha256(data[:5000]).hexdigest()
    svc.db.q(
        "insert into job_files (job_id, tenant_id, name, bytes, sha256, content_type, object_key) "
        "values (%s, %s, 'o.bin', 5000, %s, 'application/octet-stream', %s)",
        (job, svc.tenants["A"], hashlib.sha256(data[:5000]).hexdigest(), key),
    )
    svc.db.q("update jobs set state = 'succeeded', finished_at = now() where id = %s", (job,))
    r = svc.client.get(f"/v1/jobs/{job}/files/o.bin", headers=svc.h(), follow_redirects=False)
    assert r.status_code == 302 and "X-Amz-Signature=" in r.headers["location"]
    with httpx.Client() as http:
        assert http.get(r.headers["location"]).content == data[:5000]
        part = http.get(r.headers["location"], headers={"Range": "bytes=4000-"})
        assert part.status_code == 206 and part.content == data[4000:5000]
    man = svc.client.get(f"/v1/jobs/{job}/files", headers=svc.h()).json()
    assert man["files"][0]["url"].startswith(moto_endpoint) and "X-Amz-Expires=900" in man["files"][0]["url"]
    # store-level helpers
    tmp = Path(svc.settings.store_root) / "back.bin"
    svc.store.get_to_file(key, tmp)
    assert tmp.read_bytes() == data[:5000] and svc.store.head(key) == {"bytes": 5000}
    assert svc.store.list_prefix(f"t/{svc.tenants['A']}/jobs/") == [(key, 5000)]
    assert svc.store.delete_prefix(f"t/{svc.tenants['A']}/") == 2
    assert svc.store.list_prefix("t/") == []
