"""Object store (SPEC 8.4): `ObjectStore` protocol, `LocalStore` (filesystem + HMAC-signed `/v1/_store` URLs) and
`S3Store` (boto3: AWS, SeaweedFS, moto). Object keys: `t/{tenant}/uploads/{upload}/{filename}`,
`t/{tenant}/datasets/{dataset}/...`, `t/{tenant}/jobs/{job}/{name}`. Presigned URLs live <= 900 s and name one key."""

from __future__ import annotations

from queuetruth_service.settings import Settings
from queuetruth_service.store.base import (
    NotFoundError,
    ObjectStore,
    PartTooLarge,
    PartUrl,
    SizeMismatch,
    StoreError,
    dataset_prefix,
    expected_part_size,
    job_key,
    job_prefix,
    part_count,
    safe_filename,
    tenant_prefix,
    upload_key,
    upload_prefix,
)


def make_store(settings: Settings) -> ObjectStore:
    """The store selected by `QT_STORE`."""
    if settings.store == "s3":
        from queuetruth_service.store.s3 import S3Store

        return S3Store.from_settings(settings)
    from queuetruth_service.store.local import LocalStore

    return LocalStore.from_settings(settings)


__all__ = [
    "NotFoundError",
    "ObjectStore",
    "PartTooLarge",
    "PartUrl",
    "SizeMismatch",
    "StoreError",
    "dataset_prefix",
    "expected_part_size",
    "job_key",
    "job_prefix",
    "make_store",
    "part_count",
    "safe_filename",
    "tenant_prefix",
    "upload_key",
    "upload_prefix",
]
