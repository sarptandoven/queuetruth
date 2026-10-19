"""`/v1/keys`: create (secret shown once, never stored in the idempotency record), list, revoke (immediate). A tenant
holds at most `QT_MAX_ACTIVE_KEYS` unrevoked keys (security review M4; rate limits are per tenant, so extra keys add no
budget either)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request, Response

from queuetruth_service import idempotency, keys
from queuetruth_service.api_models import ApiKeyCreatedResponse, KeyCreate, KeyPage
from queuetruth_service.auth import AdminAuth, AdminReadAuth
from queuetruth_service.context import ctx
from queuetruth_service.db import tenant_tx
from queuetruth_service.documents import fetch_one, key_doc, page, page_doc
from queuetruth_service.ids import new_id
from queuetruth_service.problems import ProblemError, not_found
from queuetruth_service.retention import audit_sync

router = APIRouter(prefix="/v1/keys")
Limit = Annotated[int, Query(ge=1, le=200)]


@router.post("", status_code=201, response_model=ApiKeyCreatedResponse, summary="Create an API key")
async def create_key(request: Request, body: KeyCreate, idem: idempotency.IdemKey, auth: AdminAuth) -> Any:
    """`qt.api_key_created` (the secret appears only in this response)."""
    c = ctx(request)

    def execute(conn: Any) -> idempotency.Outcome:
        # serialize key creation per tenant so concurrent requests cannot overshoot the cap
        conn.execute("select id from tenants where id = %s for update", (auth.tenant_id,))
        active = conn.execute(
            "select count(*) from api_keys where tenant_id = %s and revoked_at is null", (auth.tenant_id,)
        ).fetchone()[0]
        limit = c.settings.max_active_keys
        if active >= limit:
            raise ProblemError(
                409,
                "key-limit",
                "Too many API keys",
                "QT-USAGE-001",
                f"the tenant has {active} active keys (limit {limit}); revoke one first",
            )
        secret, prefix = keys.generate(c.settings.env)
        kid = new_id("key")
        row = fetch_one(
            conn,
            "insert into api_keys (id, tenant_id, prefix, key_hash, scopes, name) values (%s, %s, %s, %s, %s, %s) "
            "returning *",
            (
                kid,
                auth.tenant_id,
                prefix,
                keys.key_hash(c.settings.pepper, secret),
                sorted(set(body.scopes)),
                body.name,
            ),
        )
        audit_sync(conn, auth.tenant_id, auth.key_id, "key.create", "api_key", kid)
        doc = key_doc(row or {}) | {"schema": "qt.api_key_created", "secret": secret}
        return 201, doc, {}, None

    return await idempotency.run(request, auth, idem, execute)


@router.get("", response_model=KeyPage, summary="List API keys")
def list_keys(request: Request, auth: AdminReadAuth, limit: Limit = 50, starting_after: str | None = None) -> Any:
    """`qt.page[qt.api_key]`, newest first."""
    with tenant_tx(ctx(request).db(), auth) as conn:
        rows, more = page(conn, "api_keys", auth.tenant_id, limit, starting_after)
    return page_doc([key_doc(r) for r in rows], more)


@router.delete("/{key_id}", status_code=204, response_class=Response, summary="Revoke an API key")
def revoke_key(request: Request, key_id: Annotated[str, Path(max_length=64)], auth: AdminAuth) -> Response:
    """Revocation is immediate; unknown or already revoked keys are 404."""
    with tenant_tx(ctx(request).db(), auth) as conn:
        row = fetch_one(
            conn,
            "update api_keys set revoked_at = now() where id = %s and tenant_id = %s and revoked_at is null "
            "returning id",
            (key_id, auth.tenant_id),
        )
        if row is None:
            raise not_found("key")
        audit_sync(conn, auth.tenant_id, auth.key_id, "key.revoke", "api_key", key_id)
    return Response(status_code=204)
