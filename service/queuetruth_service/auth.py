"""API-key authentication and scopes (SPEC 8.5): `Authorization: Bearer <key>` or HTTP Basic with the key as user
name and an empty password. Malformed keys and bad checksums are rejected before any database access, and so is
every client over its failed-authentication budget (`ratelimit.FailureThrottle`, L4). The key is checked by the
SECURITY DEFINER `qt_authenticate(prefix, HMAC(pepper, key))`, which also binds the key's tenant to the transaction;
the database never hands key hashes to the api. The resulting `Auth` is the binding every later transaction of the
request presents again (`tenant_tx(pool, auth)`), so a key revoked mid-request stops working at once. Scopes are
hierarchical (read < write < admin). Every authenticated request takes a token from its tenant's bucket; the headers
ride on `request.state.extra_headers`."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import Depends, Request
from psycopg.rows import dict_row

from queuetruth_service import keys, ratelimit
from queuetruth_service.context import ctx
from queuetruth_service.logs import tenant_id_var
from queuetruth_service.problems import ProblemError

SCOPE_RANK = {"read": 0, "write": 1, "admin": 2}


@dataclass(frozen=True)
class Auth:
    """The authenticated caller; also the tenant binding of its transactions (see `db.pool.KeyBinding`)."""

    key_id: str
    tenant_id: str
    scopes: tuple[str, ...]
    prefix: str = field(default="", repr=False)
    key_hash: str = field(default="", repr=False)

    def bind_query(self) -> tuple[str, tuple[Any, ...]]:
        """The binding query of this caller's key."""
        return "select tenant_id from qt_authenticate(%s, %s)", (self.prefix, self.key_hash)

    def has(self, scope: str) -> bool:
        """True when the key's scopes include `scope` (hierarchically)."""
        return max((SCOPE_RANK.get(s, -1) for s in self.scopes), default=-1) >= SCOPE_RANK[scope]


def unauthorized(detail: str = "missing or invalid API key") -> ProblemError:
    """401 problem with `WWW-Authenticate`."""
    return ProblemError(
        401,
        "unauthorized",
        "Unauthorized",
        "QT-USAGE-001",
        detail,
        headers={"WWW-Authenticate": 'Bearer realm="queuetruth"'},
    )


def extract_key(request: Request) -> str | None:
    """The key from Bearer or Basic credentials."""
    header = request.headers.get("authorization", "")
    scheme, _, cred = header.partition(" ")
    if scheme.lower() == "bearer":
        return cred.strip() or None
    if scheme.lower() == "basic":
        try:
            user, _, pw = base64.b64decode(cred.strip(), validate=True).decode().partition(":")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return None
        return user if pw == "" and user else None
    return None


def client_address(request: Request) -> str:
    """The client address used by the failed-authentication throttle."""
    return request.client.host if request.client and request.client.host else "unknown"


def authenticate(request: Request, scope: str, bucket: str | None = None) -> Auth:
    """Authenticates, checks the scope and takes a rate-limit token (429 when empty)."""
    c = ctx(request)
    throttle = c.auth_throttle
    client = client_address(request)
    if throttle is not None and (wait := throttle.retry_after(client)) is not None:
        raise ProblemError(
            429,
            "rate-limited",
            "Too many requests",
            "QT-USAGE-001",
            "too many failed authentications from this address",
            headers={"Retry-After": str(wait)},
        )
    key = extract_key(request)
    prefix = keys.parse(key) if key else None
    if prefix is None or key is None:
        if throttle is not None:
            throttle.failed(client)
        raise unauthorized()
    key_hash = keys.key_hash(c.settings.pepper, key)
    b = bucket or ("read" if request.method in ("GET", "HEAD") else "write")
    per_min, burst = c.settings.rate_limits[b]
    with c.db().connection() as conn, conn.transaction():
        row = (
            conn.cursor(row_factory=dict_row)
            .execute("select * from qt_authenticate(%s, %s)", (prefix, key_hash))
            .fetchone()
        )
        if row is None:
            if throttle is not None:
                throttle.failed(client)
            raise unauthorized()
        tenant_id_var.set(row["tenant_id"])
        auth = Auth(row["key_id"], row["tenant_id"], tuple(row["scopes"]), prefix, key_hash)
        if not auth.has(scope):
            raise ProblemError(
                403, "forbidden", "Forbidden", "QT-USAGE-001", f"this endpoint requires the {scope} scope"
            )
        decision = ratelimit.take(conn, b, per_min, burst)
        conn.execute(
            "update api_keys set last_used_at = now() where id = %s and tenant_id = %s and "
            "(last_used_at is null or last_used_at < now() - interval '1 minute')",
            (auth.key_id, auth.tenant_id),
        )
    request.state.extra_headers = {k: v for k, v in decision.headers.items() if k != "Retry-After"}
    if not decision.allowed:
        raise ProblemError(
            429,
            "rate-limited",
            "Too many requests",
            "QT-USAGE-001",
            f"rate limit for bucket {b} exceeded",
            headers=decision.headers,
        )
    return auth


def require(scope: str, bucket: str | None = None):
    """FastAPI dependency factory."""

    def dep(request: Request) -> Auth:
        return authenticate(request, scope, bucket)

    return dep


ReadAuth = Annotated[Auth, Depends(require("read"))]
WriteAuth = Annotated[Auth, Depends(require("write"))]
SubmitAuth = Annotated[Auth, Depends(require("write", bucket="submit"))]
AdminAuth = Annotated[Auth, Depends(require("admin"))]
AdminReadAuth = Annotated[Auth, Depends(require("admin", bucket="read"))]
