"""Service configuration (SPEC 8.1): environment variables with prefix `QT_`, read by `pydantic-settings`.

Secrets are `SecretStr` so they never appear in `repr`, logs or problems. Database roles (security review H1/M3): the
api connects only as `qt_app` (`QT_DATABASE_URL_APP`) and the worker only as `qt_worker` (`QT_DATABASE_URL_WORKER`)
in every `ENV`; there is no fallback to the owner DSN (`QT_DATABASE_URL`), which only `migrate` and `create-tenant`
use. Both pools also verify the role at startup (`queuetruth_service.db.pool`).

`ENV=prod` (M5) refuses placeholder or weak values (`change-me`, empty, short, few distinct characters) for every
secret it is given, requires `QT_KEY_PEPPER` for the api and `create-tenant` (`require`), `QT_SIGNING_KEY` whenever
local-store URLs are signed, and refuses `QT_FORWARDED_ALLOW_IPS=*`. In `ENV=dev` the local-store signing key, when
unset, is a random key created once per store root (`<STORE_ROOT>/.signing-key`, mode 0600), never a constant.

`S3_PUBLIC_ENDPOINT` is an addition to SPEC 8.1: SigV4 presigned URLs are bound to the host the client reaches.
"""

from __future__ import annotations

import os
import secrets
import tempfile
from functools import cached_property
from pathlib import Path
from typing import Literal

import psycopg
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_PEPPER = "qt-dev-pepper-not-for-production"
SIGNING_KEY_FILE = ".signing-key"

#: SPEC 8.7 token buckets: name -> (tokens per minute, burst). Buckets are per tenant (M4), not per API key.
DEFAULT_RATE_LIMITS: dict[str, tuple[float, float]] = {"read": (120, 60), "write": (30, 10), "submit": (10, 5)}

#: Upper bounds of the compute knobs a spec may ask for (M4): field name (`bootstrap.b` = `BootstrapSpec.b`) -> max.
DEFAULT_COMPUTE_LIMITS: dict[str, int] = {
    "threads": 16,
    "particles": 10_000,
    "bootstrap.b": 10_000,
    "bootstrap_b": 10_000,
    "perm_b": 20_000,
}

#: Job kinds that run in a child process and so take a timeout (`runners.TARGETS`).
JOB_KINDS = ("replay", "calibration", "scorecard", "report", "dataset_validate", "ingest")

#: Substrings that mark a value as a template placeholder rather than a generated secret.
PLACEHOLDERS = (
    "change-me",
    "changeme",
    "change_me",
    "replace-me",
    "replaceme",
    "placeholder",
    "example",
    "not-for-production",
    "password",
    "secret",
    "default",
    "xxxxxx",
)
MIN_KEY_CHARS = 32
MIN_PASSWORD_CHARS = 16


class ConfigError(ValueError):
    """A setting required by this process is missing or unacceptable."""


def weak_secret(value: str, min_chars: int) -> str | None:
    """Why `value` is not acceptable as a production secret, or None."""
    v = value.strip()
    if not v:
        return "is empty"
    low = v.lower()
    for p in PLACEHOLDERS:
        if p in low:
            return f"looks like a placeholder (contains {p!r})"
    if len(v) < min_chars:
        return f"is shorter than {min_chars} characters"
    if len(set(v)) < 8:
        return "has fewer than 8 distinct characters"
    return None


def dsn_password(dsn: str | None) -> str | None:
    """The password embedded in a libpq DSN or URL, if any."""
    if not dsn:
        return None
    try:
        pw = psycopg.conninfo.conninfo_to_dict(dsn).get("password")
    except psycopg.ProgrammingError:
        return None
    return str(pw) if pw is not None else None


def _per_install_key(root: Path) -> bytes:
    """The dev signing key of a store root: created once (random, 0600, atomic), then read back."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / SIGNING_KEY_FILE
    if not path.exists():
        fd, tmp = tempfile.mkstemp(dir=root, prefix=".signing-key-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(secrets.token_hex(32).encode())
            os.chmod(tmp, 0o600)
            try:
                os.link(tmp, path)  # atomic: concurrent first starts agree on one key
            except FileExistsError:
                pass
        finally:
            os.unlink(tmp)
    data = path.read_bytes().strip()
    if len(data) < 64:
        raise ConfigError(f"{path} is not a valid signing key file; delete it to create a new one")
    return data


class Settings(BaseSettings):
    """All `QT_*` settings of the api, worker and admin processes."""

    model_config = SettingsConfigDict(env_prefix="QT_", extra="ignore", frozen=True)

    env: Literal["dev", "prod"] = "dev"
    database_url: str = "postgresql:///queuetruth"
    database_url_app: str | None = None
    database_url_worker: str | None = None
    store: Literal["local", "s3"] = "local"
    store_root: str = "./.qt-store"
    s3_endpoint: str | None = None
    s3_public_endpoint: str | None = None
    s3_bucket: str = "queuetruth"
    s3_region: str = "us-east-1"
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None
    key_pepper: SecretStr | None = None
    signing_key: SecretStr | None = None
    public_base_url: str = ""
    #: Comma-separated proxy addresses/networks whose X-Forwarded-* headers the api trusts; empty = none.
    forwarded_allow_ips: str = ""
    zero_egress: bool = False
    biller: Literal["noop", "stripe"] = "noop"
    stripe_api_key: SecretStr | None = None
    stripe_api_base: str = "https://api.stripe.com"
    max_upload_bytes: int = Field(default=21_474_836_480, ge=1)
    part_size_bytes: int = Field(default=67_108_864, ge=1)
    #: Pending uploads older than this are aborted and marked failed (their parts deleted).
    pending_upload_ttl_s: int = Field(default=86_400, ge=60)
    #: Default child-process timeout of a job (SPEC 8.1); `job_timeouts_s` overrides it per job kind.
    job_timeout_s: float = Field(default=7200, gt=0)
    #: Per-kind timeouts (`QT_JOB_TIMEOUTS_S`, JSON object `{kind: seconds}`; kinds of `JOB_KINDS`); unset kinds use
    #: `job_timeout_s`.
    job_timeouts_s: dict[str, float] = Field(default_factory=dict)
    worker_concurrency: int = Field(default=2, ge=1)
    #: Running jobs per tenant across all workers (0 = no cap); excess jobs wait in the queue (M4).
    tenant_max_running: int = Field(default=1, ge=0)
    #: Delay before a job deferred by `tenant_max_running` is offered again.
    tenant_defer_s: float = Field(default=5, gt=0)
    #: Directory under which each job gets a private work directory (default: the system temp directory).
    job_root: str = ""
    retention_days_default: int = Field(default=30, ge=1, le=365)
    lease_s: int = Field(default=60, ge=1)
    heartbeat_s: float = Field(default=20, gt=0)
    sweep_interval_s: float = Field(default=30, gt=0)
    maintenance_interval_s: float = Field(default=10, gt=0)
    job_memory_bytes: int = Field(default=8 << 30, ge=0)
    max_attempts: int = Field(default=3, ge=1)
    presign_ttl_s: int = Field(default=900, ge=1, le=900)
    db_pool_max: int = Field(default=20, ge=2)
    rate_limits: dict[str, tuple[float, float]] = Field(default_factory=lambda: dict(DEFAULT_RATE_LIMITS))
    #: Failed authentications per client address: (per minute, burst); checked before any database access (L4).
    auth_failure_limit: tuple[float, float] = (30, 10)
    #: Active (unrevoked) API keys per tenant (M4).
    max_active_keys: int = Field(default=25, ge=1)
    compute_limits: dict[str, int] = Field(default_factory=lambda: dict(DEFAULT_COMPUTE_LIMITS))
    log_level: str = "INFO"
    worker_health_file: str = ""

    @model_validator(mode="after")
    def _checks(self) -> Settings:
        if self.store == "s3" and not self.s3_endpoint:
            raise ValueError("STORE=s3 requires QT_S3_ENDPOINT")
        for kind, t in self.job_timeouts_s.items():
            if kind not in JOB_KINDS:
                raise ValueError(f"QT_JOB_TIMEOUTS_S: unknown job kind {kind!r} (allowed: {', '.join(JOB_KINDS)})")
            if not t > 0:
                raise ValueError(f"QT_JOB_TIMEOUTS_S: {kind} must be > 0 seconds, got {t}")
        if self.env == "prod":
            problems = []
            for name, value, min_chars in (
                ("QT_KEY_PEPPER", self.key_pepper, MIN_KEY_CHARS),
                ("QT_SIGNING_KEY", self.signing_key, MIN_KEY_CHARS),
                ("QT_S3_SECRET_ACCESS_KEY", self.s3_secret_access_key, MIN_PASSWORD_CHARS),
                ("QT_S3_ACCESS_KEY_ID", self.s3_access_key_id, 1),
                ("QT_STRIPE_API_KEY", self.stripe_api_key, 1),
            ):
                if value is not None and (why := weak_secret(value.get_secret_value(), min_chars)):
                    problems.append(f"{name} {why}")
            for name, dsn in (
                ("QT_DATABASE_URL", self.database_url),
                ("QT_DATABASE_URL_APP", self.database_url_app),
                ("QT_DATABASE_URL_WORKER", self.database_url_worker),
            ):
                pw = dsn_password(dsn)
                if pw is not None and (why := weak_secret(pw, MIN_PASSWORD_CHARS)):
                    problems.append(f"the password in {name} {why}")
            if self.database_url_app and self.database_url_app == self.database_url:
                problems.append("QT_DATABASE_URL_APP must not be the owner DSN QT_DATABASE_URL")
            if self.database_url_worker and self.database_url_worker == self.database_url:
                problems.append("QT_DATABASE_URL_WORKER must not be the owner DSN QT_DATABASE_URL")
            if "*" in [h.strip() for h in self.forwarded_allow_ips.split(",")]:
                problems.append("QT_FORWARDED_ALLOW_IPS=* trusts X-Forwarded-* from any client; list the proxies")
            if problems:
                raise ValueError("ENV=prod refuses this configuration: " + "; ".join(problems))
        return self

    def require(self, process: Literal["api", "worker", "migrate", "create-tenant"]) -> None:
        """Raises `ConfigError` when a setting `process` needs is missing (called before a process starts)."""
        missing = []
        if process == "api":
            if self.env == "prod" and self.key_pepper is None:
                missing.append("QT_KEY_PEPPER")
            if self.env == "prod" and self.store == "local" and self.signing_key is None:
                missing.append("QT_SIGNING_KEY")
            if not self.database_url_app:
                missing.append("QT_DATABASE_URL_APP")
        elif process == "worker":
            if not self.database_url_worker:
                missing.append("QT_DATABASE_URL_WORKER")
        elif process == "create-tenant" and self.env == "prod" and self.key_pepper is None:
            missing.append("QT_KEY_PEPPER")
        if missing:
            raise ConfigError(f"{process} requires {', '.join(missing)} (ENV={self.env})")

    def job_timeout_for(self, kind: str) -> float:
        """Child-process timeout of a job of `kind` (SPEC 8.1): `job_timeouts_s[kind]`, else `job_timeout_s`."""
        return float(self.job_timeouts_s.get(kind, self.job_timeout_s))

    @cached_property
    def pepper(self) -> bytes:
        """HMAC key for API-key hashes."""
        if self.key_pepper is not None:
            return self.key_pepper.get_secret_value().encode()
        if self.env == "prod":
            raise ConfigError("ENV=prod requires QT_KEY_PEPPER")
        return DEV_PEPPER.encode()

    @cached_property
    def signing_secret(self) -> bytes:
        """HMAC key for local-store URLs: `QT_SIGNING_KEY`, else (dev only) the random per-install key."""
        if self.signing_key is not None:
            return self.signing_key.get_secret_value().encode()
        if self.env == "prod":
            raise ConfigError("ENV=prod requires QT_SIGNING_KEY to sign local-store URLs")
        return _per_install_key(Path(self.store_root))

    @property
    def app_dsn(self) -> str:
        """DSN of the `qt_app` role used by the api (required in every ENV; never the owner DSN)."""
        if not self.database_url_app:
            raise ConfigError(
                "QT_DATABASE_URL_APP is required: the api connects only as the unprivileged qt_app role "
                "(the owner DSN QT_DATABASE_URL is for migrate and create-tenant)"
            )
        return self.database_url_app

    @property
    def worker_dsn(self) -> str:
        """DSN of the `qt_worker` role used by the worker (required in every ENV; never the owner DSN)."""
        if not self.database_url_worker:
            raise ConfigError(
                "QT_DATABASE_URL_WORKER is required: the worker connects only as the unprivileged qt_worker role "
                "(the owner DSN QT_DATABASE_URL is for migrate and create-tenant)"
            )
        return self.database_url_worker

    @property
    def effective_biller(self) -> str:
        """`noop` whenever zero-egress is on (SPEC 8.8)."""
        return "noop" if self.zero_egress else self.biller
