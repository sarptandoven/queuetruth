"""S3-compatible store via boto3 (path-style SigV4, checksums only when required, so SeaweedFS, moto and presigned
PUTs from plain HTTP clients work). Presigned URLs are signed by a client bound to `S3_PUBLIC_ENDPOINT` (SigV4 signs
the Host header); signing is offline, so that client never connects.

Presigned UploadPart URLs do not bind the body length, so completion lists the uploaded parts (`ListParts`) and
refuses any layout other than the declared one (`SizeMismatch`, security review M1); the caller then aborts the
upload, which deletes its parts."""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
import botocore.exceptions
from botocore.config import Config

from queuetruth_service.settings import Settings
from queuetruth_service.store.base import NotFoundError, PartUrl, SizeMismatch, StoreError, check_key, check_part_sizes


def s3_client(endpoint: str | None, region: str, key_id: str | None, secret: str | None) -> Any:
    """A boto3 S3 client with the settings S3-compatible servers need."""
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=5,
            read_timeout=60,
        ),
    )


class S3Store:
    """`ObjectStore` on S3 / SeaweedFS / moto."""

    kind = "s3"

    def __init__(self, bucket: str, internal: Any, public: Any | None = None, ttl_s: int = 900) -> None:
        self.bucket = bucket
        self.c = internal
        self.pub = public or internal
        self.ttl_s = min(ttl_s, 900)
        self.endpoint: str | None = getattr(getattr(internal, "meta", None), "endpoint_url", None)

    @classmethod
    def from_settings(cls, s: Settings) -> S3Store:
        """Store configured from settings."""
        kid = s.s3_access_key_id.get_secret_value() if s.s3_access_key_id else None
        sec = s.s3_secret_access_key.get_secret_value() if s.s3_secret_access_key else None
        internal = s3_client(s.s3_endpoint, s.s3_region, kid, sec)
        public = s3_client(s.s3_public_endpoint, s.s3_region, kid, sec) if s.s3_public_endpoint else None
        return cls(s.s3_bucket, internal, public, s.presign_ttl_s)

    def ensure_bucket(self) -> None:
        """Creates the bucket when missing."""
        try:
            self.c.head_bucket(Bucket=self.bucket)
        except botocore.exceptions.ClientError:
            self.c.create_bucket(Bucket=self.bucket)

    def create_multipart(self, key: str, size_bytes: int, part_size: int) -> tuple[str, list[PartUrl]]:
        """Starts a multipart upload and presigns every part."""
        n = max(1, math.ceil(size_bytes / part_size))
        if n > 10_000:
            raise StoreError("more than 10,000 parts: raise PART_SIZE_BYTES")
        mp = self.c.create_multipart_upload(Bucket=self.bucket, Key=check_key(key))
        return mp["UploadId"], self.part_urls(key, mp["UploadId"], n)

    def part_urls(self, key: str, multipart_id: str, n_parts: int) -> list[PartUrl]:
        """Fresh presigned part URLs."""
        exp = datetime.now(UTC) + timedelta(seconds=self.ttl_s)
        return [
            PartUrl(
                i,
                self.pub.generate_presigned_url(
                    "upload_part",
                    Params={"Bucket": self.bucket, "Key": key, "UploadId": multipart_id, "PartNumber": i},
                    ExpiresIn=self.ttl_s,
                ),
                exp,
            )
            for i in range(1, n_parts + 1)
        ]

    def _list_parts(self, key: str, multipart_id: str) -> dict[int, int]:
        sizes: dict[int, int] = {}
        try:
            for page in self.c.get_paginator("list_parts").paginate(Bucket=self.bucket, Key=key, UploadId=multipart_id):
                for p in page.get("Parts", []):
                    sizes[int(p["PartNumber"])] = int(p["Size"])
        except botocore.exceptions.ClientError as e:
            raise StoreError(f"list parts failed: {e.response.get('Error', {}).get('Code', 'error')}") from e
        return sizes

    def complete_multipart(
        self, key: str, multipart_id: str, parts: list[dict], size_bytes: int, part_size: int
    ) -> None:
        """Completes the upload after checking every stored part's size against the declared layout (the store
        checks ETags); the completed object's size is checked again."""
        check_part_sizes(size_bytes, part_size, self._list_parts(key, multipart_id))
        try:
            self.c.complete_multipart_upload(
                Bucket=self.bucket,
                Key=key,
                UploadId=multipart_id,
                MultipartUpload={
                    "Parts": [
                        {"PartNumber": int(p["part_number"]), "ETag": p["etag"]}
                        for p in sorted(parts, key=lambda p: int(p["part_number"]))
                    ]
                },
            )
        except botocore.exceptions.ClientError as e:
            raise StoreError(f"complete failed: {e.response.get('Error', {}).get('Code', 'error')}") from e
        got = self.head(key)["bytes"]
        if got != size_bytes:
            self.c.delete_object(Bucket=self.bucket, Key=key)
            raise SizeMismatch(f"completed object has {got} bytes, the upload declared {size_bytes}")

    def staged_bytes(self, key: str, multipart_id: str) -> int:
        """Bytes held by the uploaded parts of a pending upload (billed while pending)."""
        try:
            return sum(self._list_parts(key, multipart_id).values())
        except StoreError:
            return 0

    def abort_multipart(self, key: str, multipart_id: str) -> None:
        """Aborts an unfinished upload (ignores unknown ids)."""
        try:
            self.c.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=multipart_id)
        except botocore.exceptions.ClientError:
            pass

    def put_file(self, key: str, path: Path) -> str:
        """Uploads a local file; returns its sha256."""
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        self.c.upload_file(str(path), self.bucket, check_key(key))
        return h.hexdigest()

    def get_to_file(self, key: str, path: Path) -> None:
        """Downloads an object to a local file."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        try:
            self.c.download_file(self.bucket, key, str(path))
        except botocore.exceptions.ClientError as e:
            raise NotFoundError(key) from e

    def presign_get(self, key: str, ttl_s: int = 900) -> str:
        """Presigned GET for one key (TTL capped at 900 s)."""
        return self.pub.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=min(ttl_s, self.ttl_s)
        )

    def head(self, key: str) -> dict:
        """`{bytes}` of an object."""
        try:
            return {"bytes": self.c.head_object(Bucket=self.bucket, Key=key)["ContentLength"]}
        except botocore.exceptions.ClientError as e:
            raise NotFoundError(key) from e

    def sha256(self, key: str) -> str:
        """Streaming sha256 of an object."""
        try:
            body = self.c.get_object(Bucket=self.bucket, Key=key)["Body"]
        except botocore.exceptions.ClientError as e:
            raise NotFoundError(key) from e
        h = hashlib.sha256()
        for chunk in body.iter_chunks(1 << 20):
            h.update(chunk)
        return h.hexdigest()

    def list_prefix(self, prefix: str) -> list[tuple[str, int]]:
        """`(key, bytes)` under a prefix, sorted by key."""
        out = []
        for page in self.c.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            out.extend((o["Key"], int(o["Size"])) for o in page.get("Contents", []))
        return sorted(out)

    def delete_prefix(self, prefix: str) -> int:
        """Deletes objects and aborts unfinished multipart uploads under a prefix; returns the object count."""
        n = 0
        for page in self.c.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if objs:
                self.c.delete_objects(Bucket=self.bucket, Delete={"Objects": objs, "Quiet": True})
                n += len(objs)
        try:
            ups = self.c.list_multipart_uploads(Bucket=self.bucket, Prefix=prefix).get("Uploads", [])
        except botocore.exceptions.ClientError:
            ups = []
        for u in ups:
            self.abort_multipart(u["Key"], u["UploadId"])
        return n

    def check(self) -> bool:
        """True when the bucket is reachable."""
        try:
            self.c.head_bucket(Bucket=self.bucket)
            return True
        except Exception:
            return False
