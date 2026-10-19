"""Console script `queuetruth-service`:

- `migrate [--create-bucket]`: apply SQL migrations (owner role, `QT_DATABASE_URL`), set the `qt_app` / `qt_worker`
  passwords from `QT_DATABASE_URL_APP` / `QT_DATABASE_URL_WORKER` and optionally create the bucket;
- `create-tenant --name NAME [--plan P] [--zero-egress]`: create a tenant and print its first admin key once (owner
  role);
- `worker`: run the job worker (queue consumer, sweeper, maintenance) as `qt_worker` (`QT_DATABASE_URL_WORKER`);
- `api [--host H] [--port P]`: run the API with uvicorn as `qt_app` (`QT_DATABASE_URL_APP`). X-Forwarded-* headers
  are trusted only from `QT_FORWARDED_ALLOW_IPS` (default: nobody), and uvicorn's access log goes through the
  redacting JSON formatter.

Only `migrate` and `create-tenant` use the owner DSN; the api and the worker refuse to start with a privileged role.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from datetime import timedelta
from typing import Any

import psycopg

from queuetruth_service import keys, logs
from queuetruth_service.ids import new_id
from queuetruth_service.settings import Settings


def create_tenant(
    owner_dsn: str, settings: Settings, name: str, plan: str = "dev", zero_egress: bool = False
) -> dict[str, str]:
    """Creates a tenant and its first admin key; returns `{tenant_id, key_id, key}`."""
    tid, kid = new_id("ten"), new_id("key")
    secret, prefix = keys.generate(settings.env)
    with psycopg.connect(owner_dsn) as conn, conn.transaction():
        conn.execute("select set_config('app.tenant_id', %s, true)", (tid,))
        conn.execute(
            "insert into tenants (id, name, plan, zero_egress, retention_uploads_days, retention_outputs_days) "
            "values (%s, %s, %s, %s, %s, %s)",
            (tid, name, plan, zero_egress, settings.retention_days_default, settings.retention_days_default),
        )
        conn.execute(
            "insert into api_keys (id, tenant_id, prefix, key_hash, scopes, name) values (%s, %s, %s, %s, %s, %s)",
            (kid, tid, prefix, keys.key_hash(settings.pepper, secret), ["admin"], "bootstrap"),
        )
        conn.execute(
            "insert into audit_log (id, tenant_id, actor_key_id, action, resource_type, resource_id) "
            "values (%s, %s, null, 'key.create', 'api_key', %s)",
            (new_id("aud"), tid, kid),
        )
    return {"tenant_id": tid, "key_id": kid, "key": secret}


async def run_worker(settings: Settings, stop: asyncio.Event | None = None) -> None:
    """Worker process main loop."""
    from queuetruth_service.billing import make_biller
    from queuetruth_service.db.pool import make_async_pool
    from queuetruth_service.queue import PgQueuerJobQueue
    from queuetruth_service.store import make_store
    from queuetruth_service.worker import Worker

    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    settings.require("worker")
    # make_async_pool refuses superusers, BYPASSRLS roles, schema owners and roles that cannot claim jobs (H1/M3)
    pool = await make_async_pool(
        settings.worker_dsn, max_size=max(4, settings.worker_concurrency * 3), purpose="worker"
    )
    queue = PgQueuerJobQueue(settings.worker_dsn, heartbeat_timeout=timedelta(seconds=max(30, settings.lease_s)))
    worker = Worker(settings, pool, make_store(settings), queue, biller=make_biller(settings))
    try:
        await worker.serve(stop)
    finally:
        await queue.close()
        await pool.close()


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    p = argparse.ArgumentParser(prog="queuetruth-service", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    mg = sub.add_parser("migrate", help="apply database migrations")
    mg.add_argument("--create-bucket", action="store_true", help="create the S3 bucket when STORE=s3")
    ct = sub.add_parser("create-tenant", help="create a tenant and print its first admin key once")
    ct.add_argument("--name", required=True)
    ct.add_argument("--plan", default="dev")
    ct.add_argument("--zero-egress", action="store_true")
    sub.add_parser("worker", help="run the job worker")
    ap = sub.add_parser("api", help="run the HTTP API (uvicorn)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    args = p.parse_args(argv)
    try:
        settings = Settings()
    except ValueError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2
    logs.setup(settings.log_level)
    try:
        settings.require(args.cmd)
    except ValueError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2
    if args.cmd == "migrate":
        from queuetruth_service.db.runner import migrate

        applied = migrate(settings.database_url, settings.database_url_app, settings.database_url_worker)
        if args.create_bucket and settings.store == "s3":
            from queuetruth_service.store.s3 import S3Store

            S3Store.from_settings(settings).ensure_bucket()
        print(json.dumps({"applied": applied}))
        return 0
    if args.cmd == "create-tenant":
        out = create_tenant(settings.database_url, settings, args.name, args.plan, args.zero_egress)
        print(json.dumps(out))
        return 0
    if args.cmd == "worker":
        from queuetruth_service.db.pool import RoleError

        try:
            asyncio.run(run_worker(settings))
        except RoleError as e:
            print(f"configuration error: {e}", file=sys.stderr)
            return 2
        return 0
    import uvicorn

    from queuetruth_service.app import create_app

    uvicorn.run(create_app(settings), host=args.host, port=args.port, **uvicorn_options(settings))
    return 0


def uvicorn_options(settings: Settings) -> dict[str, Any]:
    """uvicorn options of the `api` command: X-Forwarded-* trusted only from `QT_FORWARDED_ALLOW_IPS` (none by
    default), no server header, and no uvicorn log config (so `logs.setup` redacts the access log)."""
    trusted = settings.forwarded_allow_ips.strip()
    return {
        "proxy_headers": bool(trusted),
        "forwarded_allow_ips": trusted or None,
        "server_header": False,
        "log_config": None,
    }


if __name__ == "__main__":
    sys.exit(main())
