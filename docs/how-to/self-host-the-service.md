# Self-host the queuetruth service

**Goal:** run the queuetruth HTTP service on your own machines, so a team can share datasets and job history, CI can
submit jobs to one endpoint, and agents can reach it through MCP, without the data leaving your network.

The service is the `queuetruth-service` package (Apache-2.0). It runs the same `queuetruth` engine as the CLI: every
job calls the [Python API](../reference/python-api.md) in a child process. Use the CLI alone if one person works on
one machine; self-host when you need shared state, access control, or a zero-egress deployment.

## Architecture

```text
 clients (queuetruth CLI, Python Client, MCP server, CI)
        |  HTTPS (TLS at your reverse proxy)
        v
 +--------------+    enqueue job id     +-----------------------------+
 |  api         | --------------------> |  worker                      |
 |  FastAPI on  |                       |  PgQueuer consumer, sweeper, |
 |  uvicorn     |                       |  maintenance; one spawned    |
 +--------------+                       |  child process per job       |
        |  qt_app role, RLS              +-----------------------------+
        v                                        | qt_worker  |
 +--------------------------+                    |            |
 |  PostgreSQL 16           | <------------------+            |
 |  metadata, queue,        |                                 v
 |  rate limits, audit log  |            +-------------------------------+
 +--------------------------+            |  object store: S3-compatible   |
                                         |  (SeaweedFS in compose, AWS S3) |
   clients upload and download --------> |  or a local directory           |
   through presigned URLs                +-------------------------------+
```

- `api` serves `/v1` (see [HTTP API](../reference/http-api.md)) and the OpenAPI 3.1 document at `/v1/openapi.json`.
- `worker` claims jobs from a Postgres-backed queue (PgQueuer), downloads the inputs into a private temporary
  directory, runs the job in a spawned child process with a timeout (`QT_JOB_TIMEOUTS_S` per job kind, else
  `QT_JOB_TIMEOUT_S`), a memory cap
  (`QT_JOB_MEMORY_BYTES`, Linux only) and, in zero-egress mode, a network guard, then uploads the outputs and commits
  the result. It also runs the sweeper (requeues jobs whose lease expired) and the maintenance passes (upload
  verification, idempotency pruning, usage push, daily retention).
- The api and the worker use two different unprivileged database roles: the api connects only as `qt_app`
  (`QT_DATABASE_URL_APP`), the worker only as `qt_worker` (`QT_DATABASE_URL_WORKER`). Neither is a superuser or has
  `BYPASSRLS`, and both processes refuse to start with any other kind of role. The owner DSN (`QT_DATABASE_URL`) is
  used only by `migrate` and `create-tenant`. A request is bound to a tenant only through the API key it presents,
  and a job only through the attempt that holds it, so a leaked `qt_app` password does not open other tenants' data
  (migration `0005_security.sql`, regression tests in `service/tests/security/`).
- Postgres holds every table, the queue and the rate-limit buckets. The suite runs against PostgreSQL 16
  (`test_cluster_is_private_pg16` in `service/tests/test_db.py`); compose pins `postgres:16.14-bookworm`.
- The object store holds uploads, datasets and job outputs under per-tenant prefixes `t/{tenant_id}/...`.

## Quick start with Docker Compose

The stack in `deploy/compose/docker-compose.yml` (project name `queuetruth`) has five services:

| Service | Image | Role | Port on the host |
|---|---|---|---|
| `postgres` | `postgres:16.14-bookworm` | database (volume `pgdata`) | not published |
| `seaweedfs` | `chrislusf/seaweedfs:4.47` | S3 gateway (volume `seaweed`) | `${QT_S3_BIND:-127.0.0.1}:${QT_S3_PORT:-8333}` |
| `migrate` | `queuetruth:${QT_VERSION:-dev}` | one shot: `queuetruth-service migrate --create-bucket` (owner role) | none |
| `api` | `queuetruth:${QT_VERSION:-dev}` | `queuetruth-service api --host 0.0.0.0 --port 8080` (role `qt_app`) | `${QT_API_BIND:-127.0.0.1}:${QT_API_PORT:-8080}` |
| `worker` | `queuetruth:${QT_VERSION:-dev}` | `queuetruth-service worker` (role `qt_worker`) | none |

Both published ports bind to `127.0.0.1` by default; set `QT_API_BIND` and `QT_S3_BIND` (for example `0.0.0.0`)
only when other machines must reach them, and then put a TLS proxy in front. Compose sets `QT_ENV=prod`, so every
secret is checked as described in the [production checklist](#production-checklist).

`api` and `worker` start only after `migrate` completed successfully. The three application containers run read-only
with a 64 MB `/tmp` tmpfs (job work directories, each with its own `tmp/`, live on the `jobs` volume instead), `cap_drop: [ALL]` and `no-new-privileges`, as uid 10001. The image is built from
`deploy/docker/Dockerfile` (multi-stage: a Rust stage builds the wheels, a slim Python 3.13 stage installs them).
These properties are checked statically by `service/tests/test_compose.py`. `scripts/compose_smoke.sh`
(`make compose-smoke`) runs the whole stack end to end with throwaway secrets (up, tenant, upload the synthetic
dataset, replay job, sha256-verified download, down); it skips when no Docker daemon is running, and the docs check
does not run the compose commands below.

### Configure the secrets

Copy the example environment file and replace every value. `.env.example` lists the seven required secrets
(`QT_PG_PASSWORD`, `QT_APP_PASSWORD`, `QT_WORKER_PASSWORD`, `QT_S3_ACCESS_KEY_ID`, `QT_S3_SECRET_ACCESS_KEY`,
`QT_KEY_PEPPER`, `QT_SIGNING_KEY`); compose refuses to start while one is unset, and with `QT_ENV=prod` the services
refuse placeholder, empty or short values (rules in the [production checklist](#production-checklist)). Generate
each value separately:

<!-- check-docs: run -->
```sh
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

<!-- check-docs: skip (needs a checkout of the repository) -->
```sh
cp deploy/compose/.env.example deploy/compose/.env
$EDITOR deploy/compose/.env    # one generated value per secret; never commit this file
```

Optional variables in `.env`: `QT_PUBLIC_BASE_URL` (default `http://localhost:8080`), `QT_S3_PUBLIC_ENDPOINT`
(default `http://localhost:8333`; presigned upload and download URLs point here, so clients must reach it),
`QT_ZERO_EGRESS` (default `false`), `QT_WORKER_CONCURRENCY` (2), `QT_TENANT_MAX_RUNNING` (1; raise it on a
single-tenant install, see below), `QT_JOB_TIMEOUT_S` (7200), `QT_JOB_MEMORY_BYTES` (8 GiB),
`QT_RETENTION_DAYS_DEFAULT` (30), `QT_API_BIND` and `QT_S3_BIND` (`127.0.0.1`), `QT_API_PORT` (8080), `QT_S3_PORT`
(8333), `QT_FORWARDED_ALLOW_IPS` (empty: trust no proxy), `QT_API_MEM_LIMIT` (2g), `QT_WORKER_MEM_LIMIT` (18g; at least
`QT_WORKER_CONCURRENCY` times `QT_JOB_MEMORY_BYTES` plus headroom), `QT_VERSION` (image tag), `QT_JOB_TIMEOUTS_S`
(per-kind job timeouts as JSON, default `{}`; see the configuration reference).

### Start the stack

Build and start the stack, then check health:

<!-- check-docs: skip (needs Docker) -->
```sh
docker compose -f deploy/compose/docker-compose.yml --env-file deploy/compose/.env up -d --build
docker compose -f deploy/compose/docker-compose.yml --env-file deploy/compose/.env ps
curl -s http://localhost:8080/v1/health
```

`GET /v1/health` needs no key and returns a `qt.health` document (`status` `ok` or `degraded`, plus `db`, `store`,
`queue` and `version`); it answers 503 when a dependency is down (`test_health_ok_and_degraded` in
`service/tests/test_api_misc.py`).

### Create a tenant and the first key

Create a tenant with the `migrate` service, the only one that holds the owner DSN (the api container has only the
`qt_app` role and cannot create tenants). The command prints the tenant's first admin key once, as JSON
`{tenant_id, key_id, key}`; store the key in your secret manager, because the service keeps only an HMAC of it.

<!-- check-docs: skip (needs Docker) -->
```sh
docker compose -f deploy/compose/docker-compose.yml --env-file deploy/compose/.env \
    run --rm migrate queuetruth-service create-tenant --name desk
```

The admin commands of the image:

<!-- check-docs: run -->
```sh
queuetruth-service --help
queuetruth-service create-tenant --help
```

| Command | What it does |
|---|---|
| `queuetruth-service migrate [--create-bucket]` | applies the SQL migrations as the owner role (`QT_DATABASE_URL`), sets the `qt_app` and `qt_worker` passwords from `QT_DATABASE_URL_APP` and `QT_DATABASE_URL_WORKER`, and with `--create-bucket` creates the bucket when `QT_STORE=s3` |
| `queuetruth-service create-tenant --name NAME [--plan P] [--zero-egress]` | creates a tenant (plan default `dev`) and its first key with scope `admin`; needs the owner DSN (and `QT_KEY_PEPPER` in prod) |
| `queuetruth-service worker` | runs the worker as `qt_worker` (`QT_DATABASE_URL_WORKER` required) |
| `queuetruth-service api [--host H] [--port P]` | runs the API with uvicorn as `qt_app` (defaults `127.0.0.1`, `8080`; `QT_DATABASE_URL_APP` required) |

### Create keys for people, CI and agents

Create narrower keys with the admin key. v0.1 has no CLI command for keys; use the HTTP API or the Python client:

<!-- check-docs: skip (needs a running service) -->
```python
from queuetruth.client import Client

admin = Client(base_url="http://localhost:8080", api_key="qt_live_...")  # the bootstrap admin key
ci = admin.create_key(["write"], "ci")
agent = admin.create_key(["read"], "agent")
print(ci.secret, agent.secret)  # shown once; a replayed request returns secret = null
```

A tenant may hold at most `QT_MAX_ACTIVE_KEYS` unrevoked keys (default 25). Scopes are hierarchical: `read` <
`write` < `admin`. `write` can upload, register datasets and submit jobs;
`admin` can also manage keys, retention and tombstones. Revoke a key with `DELETE /v1/keys/{key_id}`
(`Client.revoke_key`); revocation is immediate (`test_basic_auth_security_headers_and_revocation` in
`service/tests/test_keys_auth.py`).

## Submit a replay and download the files

The CLI's `jobs` commands talk to the service named by `QT_API_URL` (default `http://127.0.0.1:8080`) with the key in
`QT_API_KEY`. Service specs reference data by id, never by local path, and leave `out` unset (a non-null `out` is
rejected with 422).

<!-- check-docs: skip (needs a running service) -->
```sh
export QT_API_URL=http://localhost:8080
export QT_API_KEY=qt_live_...            # a write key
cat > replay.json <<'JSON'
{"dataset": {"dataset_id": "ds_01JABCDEFGHJKMNPQRSTVWXYZ0"}, "shadows": {"upload_id": "up_01JABCDEFGHJKMNPQRSTVWXYZ0"}}
JSON
queuetruth jobs submit replay --spec replay.json --wait --output-format json > job.json
JOB=$(python -c 'import json; print(json.load(open("job.json"))["id"])')
queuetruth jobs files "$JOB"
queuetruth jobs download "$JOB" --out out/
```

`jobs submit KIND` accepts `replay`, `zoo`, `calibration`, `scorecard` and `report`, sends an idempotency key (yours with
`--idempotency-key`, else a generated one reused on retries) and prints the `qt.job` document. With `--wait` it polls
until the job is terminal; a failed, canceled or expired job exits with code 2. `jobs status ID`, `jobs wait ID`,
`jobs files ID` and `jobs download ID --out DIR` complete the set; `download` verifies each file's size and sha256
against the file manifest and resumes interrupted downloads with HTTP ranges. Job files expire with the tenant's output
retention (`files_expire_at` on the job). A real replay job on `tests/fixtures/synth_small` through the API, the
worker and the child process produces the same summary as a local `qt.replay` run
(`test_real_replay_job_on_synth_small` in `service/tests/test_integration_replay.py`).

### Retries and idempotency keys

Every `POST` that creates or changes something (datasets, uploads and their completion, keys, job submissions and
cancels) requires an `Idempotency-Key` header of 1 to 255 characters; `DELETE /v1/datasets/{id}` and `PUT /v1/retention`
accept one. The same key with the
same method, path and body replays the stored response with `Idempotent-Replayed: true` and creates no second job; the
same key with a different body is 422 `idempotency-key-reused`; a duplicate that arrives while the first request is
still running is 409 `idempotency-in-flight` (O-API-2, `service/tests/test_idempotency.py`). Records are kept per
tenant and pruned after 24 hours.

An error the handler raises, any 4xx problem or a 5xx, is not stored: its transaction is rolled back and the key is
released, so you can retry with the same key once the cause is fixed. Those requests committed nothing, so there is
no effect for a stored reply to protect, and storing them would pin a transient 404 or 409 (for example a dataset
that was not ready yet) to the key for 24 hours (`test_validation_and_handler_errors_are_not_stored`). A 4xx that is
the request's outcome rather than an error, such as `upload-size-mismatch`, which records the failed upload, is
stored and replayed like a success. Secrets in a response (`secret` fields) are never stored; a replay returns
`null` for them.

The service runs replays, zoos, calibrations, scorecards and reports. It has no endpoints for features, hazard
fitting, live import, why traces, the referee, `zoo-toxicity`, `dq`, certificates, the forecast referee, the
prediction-market kit or the agent sandbox; run those with the local CLI.

## Upload data and register datasets

Data reaches the service in two steps.

1. **Upload** (`POST /v1/uploads` with `purpose`, `filename`, `size_bytes`, `sha256`): the response carries one
   presigned PUT URL per part (`QT_PART_SIZE_BYTES`, default 64 MiB; at most 10,000 parts; total at most
   `QT_MAX_UPLOAD_BYTES`, default 20 GiB). The client PUTs the parts directly to the object store, then calls
   `POST /v1/uploads/{id}/complete` with the ETags. The worker then hashes the object; the upload becomes `verified`
   or `failed`. `purpose` is one of `market_data`, `live_log`, `sim_log`, `shadow_spec`, `model`, `other`.
2. **Register a dataset** (`POST /v1/datasets` with `name`, `kind` `mbo` or `l2`, `format`, `venue`, `upload_ids` and
   optional `ingest` options, the `IngestSpec` fields without `inputs` and `out`). Every upload must be verified first
   (409 `upload-not-verified` otherwise). The service starts a `dataset_validate` job that runs `queuetruth.ingest`;
   the dataset becomes `ready` with its manifest and data-quality verdict when the job succeeds
   (`test_dataset_register_use_and_delete` in `service/tests/test_retention.py`).

Directory inputs that are not datasets (a canonical live log for `ScorecardSpec.live`, a simulator log for
`sim.log`) are uploaded as one `.zip`, `.tar`, `.tar.gz` or `.tgz` archive. The worker downloads it and the job's
child process extracts it into the job's private directory, with path and decompression-ratio guards. Dataset
registration extracts archives only for `format` `parquet`: a dataset that queuetruth already normalized (for example
the output of `queuetruth fixtures synth`) can be uploaded as one archive of its directory and registered with
`format` `parquet` (uploading its `qt.mbo` partition files and `instruments.parquet` one by one also works). For the
other formats, upload the source files themselves.

The Python client does the multipart transfer for you:

<!-- check-docs: skip (needs a running service) -->
```python
from pathlib import Path

from queuetruth.client import Client

c = Client()  # QT_API_URL, QT_API_KEY
files = sorted(Path("synth/mbo").rglob("*.parquet")) + [Path("synth/instruments.parquet")]
ups = [c.upload_file(p, "market_data") for p in files]
ups = [c.wait_upload(u.id) for u in ups]           # verified or failed
ds = c.create_dataset("synth", "mbo", "parquet", "cme", [u.id for u in ups])
job = c.jobs.wait(ds.job_id, raise_on_failure=True)
print(c.get_dataset(ds.id).status)                  # ready
sched = c.wait_upload(c.upload_file("shadows.parquet", "shadow_spec").id)
sub = c.submit("replay", {"dataset": {"dataset_id": ds.id}, "shadows": {"upload_id": sched.id}})
print(sub.id, sub.job.id)
```

Deleting a dataset or an upload (`DELETE /v1/datasets/{id}`, `DELETE /v1/uploads/{id}`) removes its objects and rows
through a deletion job and writes a tombstone. Tombstone ids have the prefix `del_` (`del_<ULID>`, like `ds_` and
`up_` for datasets and uploads); migration `0006_tombstone_ids.sql` renames tombstones written by earlier builds as
`tomb_...` (`test_tombstone_ids_follow_the_id_grammar`).

## Configuration reference

All settings are environment variables with the prefix `QT_`, read by the api, the worker and the admin commands
(`service/queuetruth_service/settings.py`). Secrets are never printed in `repr`, logs or problem documents
(`test_prod_requires_secrets_and_secrets_hidden` in `service/tests/test_settings_admin.py`).

| Variable | Default | Meaning |
|---|---|---|
| `QT_ENV` | `dev` | `dev` or `prod`. `prod` checks every secret it is given (see the checklist), requires `QT_KEY_PEPPER` for the api and `create-tenant` and `QT_SIGNING_KEY` for a local store, refuses `QT_FORWARDED_ALLOW_IPS=*`, and issues `qt_live_` keys (`qt_test_` in `dev`) |
| `QT_DATABASE_URL` | `postgresql:///queuetruth` | owner role DSN, used only by `migrate` and `create-tenant` |
| `QT_DATABASE_URL_APP` | unset | DSN of the `qt_app` role; required by the api in every `ENV`, never the owner DSN; its password is applied by `migrate` |
| `QT_DATABASE_URL_WORKER` | unset | DSN of the `qt_worker` role; required by the worker in every `ENV`, never the owner DSN; its password is applied by `migrate` |
| `QT_STORE` | `local` | `local` (a directory) or `s3` |
| `QT_STORE_ROOT` | `./.qt-store` | directory of the local store (the image sets `/tmp/qt-store`) |
| `QT_S3_ENDPOINT` | unset | S3 endpoint the api and worker use; required when `QT_STORE=s3` |
| `QT_S3_PUBLIC_ENDPOINT` | unset | S3 endpoint clients reach; presigned URLs are signed for this host (not in SPEC 8.1) |
| `QT_S3_BUCKET` | `queuetruth` | bucket name |
| `QT_S3_REGION` | `us-east-1` | region for SigV4 |
| `QT_S3_ACCESS_KEY_ID`, `QT_S3_SECRET_ACCESS_KEY` | unset | store credentials |
| `QT_KEY_PEPPER` | unset (dev fallback) | HMAC key for stored API-key hashes; changing it invalidates every key |
| `QT_SIGNING_KEY` | unset (dev fallback) | HMAC key for local-store `/v1/_store/...` URLs |
| `QT_PUBLIC_BASE_URL` | empty | base URL that local-store URLs start with (the URL clients use for the api) |
| `QT_FORWARDED_ALLOW_IPS` | empty | comma-separated addresses or networks of the reverse proxies whose `X-Forwarded-*` headers the api trusts; empty trusts none; `*` is refused in prod |
| `QT_ZERO_EGRESS` | `false` | zero-egress mode for every tenant (see the checklist) |
| `QT_BILLER` | `noop` | `noop` or `stripe`; forced to `noop` in zero-egress mode |
| `QT_STRIPE_API_KEY` | unset | only for `QT_BILLER=stripe`; leave unset when self-hosting |
| `QT_STRIPE_API_BASE` | `https://api.stripe.com` | Stripe base URL (tests) |
| `QT_MAX_UPLOAD_BYTES` | `21474836480` (20 GiB) | largest upload |
| `QT_PART_SIZE_BYTES` | `67108864` (64 MiB) | multipart part size |
| `QT_PENDING_UPLOAD_TTL_S` | `86400` | pending uploads older than this are aborted, marked failed and their parts deleted (at least 60) |
| `QT_JOB_TIMEOUT_S` | `7200` | wall-clock limit per job for kinds not listed in `QT_JOB_TIMEOUTS_S`; the child is killed after it (`test_job_timeout_kills_child`) |
| `QT_JOB_TIMEOUTS_S` | `{}` | per-kind wall-clock limits as a JSON object `{kind: seconds}`, for example `{"calibration": 14400, "report": 600}`; kinds are `replay`, `calibration`, `scorecard`, `report`, `dataset_validate`, `ingest` and `zoo`, and a kind without an entry uses `QT_JOB_TIMEOUT_S`. An unknown kind or a value that is not above 0 is a configuration error at startup (`test_job_timeout_per_kind`). An empty string is not valid JSON; leave the variable unset or use `{}` |
| `QT_WORKER_CONCURRENCY` | `2` | jobs run at once per worker process |
| `QT_TENANT_MAX_RUNNING` | `1` | running jobs per tenant across all workers (`0` = no cap); a tenant's further jobs wait in the queue. Raise it on a single-tenant install, or one tenant's jobs run one at a time |
| `QT_TENANT_DEFER_S` | `5` | delay before a job held back by `QT_TENANT_MAX_RUNNING` is offered again |
| `QT_JOB_ROOT` | system temp directory | directory under which each job gets a private work directory (compose: the `jobs` volume) |
| `QT_RETENTION_DAYS_DEFAULT` | `30` | retention of uploads and outputs for new tenants (1 to 365) |
| `QT_LEASE_S` | `60` | job lease; a job whose lease expires is requeued by the sweeper |
| `QT_HEARTBEAT_S` | `20` | how often the worker extends the lease |
| `QT_SWEEP_INTERVAL_S` | `30` | sweeper interval |
| `QT_MAINTENANCE_INTERVAL_S` | `10` | maintenance interval (upload verification, idempotency pruning, usage push; retention once per UTC day) |
| `QT_JOB_MEMORY_BYTES` | `8589934592` (8 GiB) | `RLIMIT_AS` of each job child on Linux; `0` disables it |
| `QT_MAX_ATTEMPTS` | `3` | attempts before a lost job fails with `QT-ENGINE-001` ("worker lost") |
| `QT_PRESIGN_TTL_S` | `900` | presigned URL lifetime in seconds (at most 900) |
| `QT_DB_POOL_MAX` | `20` | api connection pool size |
| `QT_RATE_LIMITS` | `{"read": [120, 60], "write": [30, 10], "submit": [10, 5]}` | token buckets per tenant as JSON: tokens per minute and burst |
| `QT_AUTH_FAILURE_LIMIT` | `[30, 10]` | failed authentications per client address: per minute and burst, checked before any database access |
| `QT_MAX_ACTIVE_KEYS` | `25` | unrevoked API keys per tenant |
| `QT_COMPUTE_LIMITS` | `{"threads": 16, "particles": 10000, "bootstrap.b": 10000, "bootstrap_b": 10000, "perm_b": 20000}` | upper bounds of the compute knobs a job spec may ask for, as JSON; a spec above a bound is rejected |
| `QT_LOG_LEVEL` | `INFO` | log level |
| `QT_WORKER_HEALTH_FILE` | empty | file the worker touches every 10 s (compose health check) |

Compose-only variables (read by `docker-compose.yml`, not by the service): `QT_PG_PASSWORD`, `QT_APP_PASSWORD`,
`QT_WORKER_PASSWORD` (assembled into the three DSNs), `QT_API_BIND`, `QT_S3_BIND`, `QT_API_PORT`, `QT_S3_PORT`,
`QT_API_MEM_LIMIT`, `QT_WORKER_MEM_LIMIT`, `QT_VERSION`.

To print every setting and its default from the installed version:

<!-- check-docs: run -->
```python
from queuetruth_service.settings import Settings

for name, field in Settings.model_fields.items():
    default = field.default_factory() if field.default_factory else field.default
    print(f"QT_{name.upper():28} {default!r}")
```

## Production checklist

- **`QT_ENV=prod` and secrets.** In prod every secret the process is given must be a generated value: keys
  (`QT_KEY_PEPPER`, `QT_SIGNING_KEY`) at least 32 characters, passwords (`QT_S3_SECRET_ACCESS_KEY` and the passwords
  inside the three DSNs) at least 16, at least 8 distinct characters, and no placeholder text such as `change-me`,
  `example`, `password`, `secret` or `default`. `QT_DATABASE_URL_APP` and `QT_DATABASE_URL_WORKER` must differ from the
  owner DSN, and `QT_FORWARDED_ALLOW_IPS=*` is refused. The process exits with code 2 before touching the database when
  a setting is missing or refused:

  <!-- check-docs: run exit=2 -->
  ```sh
  # both commands stop with "configuration error" and exit code 2, before any database connection
  env -u QT_KEY_PEPPER -u QT_SIGNING_KEY -u QT_DATABASE_URL_APP QT_ENV=prod queuetruth-service api || rc=$?
  test "${rc:-0}" -eq 2
  QT_ENV=prod QT_KEY_PEPPER=change-me queuetruth-service worker
  ```

- **Database roles and row-level security.** Migrations and `create-tenant` run as the owner role; the api connects
  as `qt_app` and the worker as `qt_worker` (`NOSUPERUSER NOBYPASSRLS`, own nothing); each process verifies its role
  at startup. Every tenant table has `FORCE ROW LEVEL SECURITY`. The tenant of a transaction is bound only by
  `SECURITY DEFINER` functions that check an API key or a held job attempt, and is signed so that setting it by hand
  binds nothing (`0005_security.sql`, `service/tests/security/`). Ids of another tenant return 404 on every endpoint
  (O-API-3, `service/tests/test_tenancy.py`). Keep the owner DSN out of the api and worker environments.
- **TLS at a reverse proxy.** The api serves plain HTTP. Put it behind a proxy that terminates TLS, set
  `QT_PUBLIC_BASE_URL` and `QT_S3_PUBLIC_ENDPOINT` to the HTTPS names clients use, and restrict who can reach the
  proxy. The api trusts `X-Forwarded-For` and `X-Forwarded-Proto` only from the addresses in
  `QT_FORWARDED_ALLOW_IPS` (none by default); list your proxies there. Keep `QT_API_BIND` and `QT_S3_BIND` on
  `127.0.0.1` unless the proxy runs on another host.
- **Keys and scopes.** Give each person, CI job and agent its own key with the smallest scope; agents that only read
  get `read`. Keys are `qt_live_<32 base62>_<6 base62 checksum>`; a bad checksum is rejected before any database
  lookup, and keys are redacted from logs (`test_key_format_checksum_and_hash`, `test_json_logs_redact_keys`).
- **Retention and tombstones.** Uploads and job outputs expire after the tenant's retention (default 30 days; change
  it with `PUT /v1/retention`, admin scope). The daily purge deletes objects and rows, marks jobs `expired` and
  writes tombstones (ids, object counts, sha256s, time; no data), listed by `GET /v1/tombstones`. `DELETE`
  endpoints hard-delete through a deletion job. The audit log records key creation and revocation, deletions and
  retention changes (O-RET-1, `service/tests/test_retention.py`).
- **Zero-egress mode.** Set `QT_ZERO_EGRESS=true` for the whole service, or create single tenants with
  `create-tenant --zero-egress`. Then the biller is `noop`, and each job's child process installs a guard
  (`queuetruth_service.netguard`) that patches `socket.connect`, `connect_ex`, `sendto` and `create_connection` so
  only the database host and port and the store endpoint can be reached; Unix sockets stay allowed (O-EGRESS-1,
  `service/tests/test_egress.py`). The guard applies to job children, not to the api or the worker parent process,
  and it does not intercept name resolution done inside libc. To verify: `GET /v1/meta` reports `zero_egress: true`
  for the tenant, and the guard's behavior can be checked offline:

<!-- check-docs: run -->
```python
import socket

from queuetruth_service import netguard

allow = netguard.allowlist_from("postgresql://qt_app:pw@127.0.0.1:5432/queuetruth", "http://127.0.0.1:8333")
print(allow)
netguard.install(allow)
try:
    socket.create_connection(("192.0.2.10", 443), timeout=1)
except netguard.EgressBlocked as e:
    print("blocked:", e)
```

  For a network-level check, also deny egress from the worker host in your firewall and watch for blocked attempts.
- **Memory cap.** `QT_JOB_MEMORY_BYTES` is applied with `RLIMIT_AS` on Linux only; on macOS it is a no-op. Size it
  below the container or host memory.
- **Backups.** Back up the Postgres database (for example `pg_dump -Fc`) and the bucket (any S3 sync tool) together,
  database first. A restore from before a deletion brings the deleted data back; delete it again and keep the
  tombstones. Backup and restore are not exercised by the test suite.
- **Health and logs.** Monitor `GET /v1/health` (no auth) and the worker's health file. Both processes log one JSON
  object per line on stderr with `request_id`, `tenant_id` and `job_id`, never request bodies or data rows. Every
  response carries `X-Request-Id`; quote it when reporting a problem.
- **Per-tenant fairness.** `QT_TENANT_MAX_RUNNING` (default 1) caps a tenant's running jobs across all workers,
  `QT_COMPUTE_LIMITS` caps threads, particles and bootstrap sizes per job, and `QT_MAX_ACTIVE_KEYS` caps keys. On a
  single-tenant install raise `QT_TENANT_MAX_RUNNING` to `QT_WORKER_CONCURRENCY` times the number of workers, or jobs
  run one at a time.
- **Rate limits.** Per-tenant token buckets: `read` 120/min (burst 60), `write` 30/min (burst 10), `submit` 10/min
  (burst 5); failed authentications are limited per client address (`QT_AUTH_FAILURE_LIMIT`) before any database
  access. A 429 carries `Retry-After` and draft-11 `RateLimit-Policy` / `RateLimit` headers (O-API-6,
  `service/tests/test_ratelimit.py`).

## Run without Docker

Install both wheels into a virtual environment. They are built from a checkout the same way the Dockerfile does it
(`uv build --wheel .` for `queuetruth`, `uv build --wheel service` for `queuetruth-service`).

<!-- check-docs: skip (needs PostgreSQL and the wheels) -->
```sh
python3 -m venv /opt/queuetruth
/opt/queuetruth/bin/pip install wheels/queuetruth-*.whl wheels/queuetruth_service-*.whl
export QT_ENV=prod
export QT_DATABASE_URL=postgresql://postgres:OWNER_PASSWORD@db.internal:5432/queuetruth
export QT_DATABASE_URL_APP=postgresql://qt_app:APP_PASSWORD@db.internal:5432/queuetruth
export QT_DATABASE_URL_WORKER=postgresql://qt_worker:WORKER_PASSWORD@db.internal:5432/queuetruth
export QT_STORE=local QT_STORE_ROOT=/var/lib/queuetruth/store
export QT_PUBLIC_BASE_URL=https://queuetruth.internal
export QT_KEY_PEPPER=... QT_SIGNING_KEY=...
/opt/queuetruth/bin/queuetruth-service migrate
/opt/queuetruth/bin/queuetruth-service create-tenant --name desk
env -u QT_DATABASE_URL -u QT_DATABASE_URL_WORKER /opt/queuetruth/bin/queuetruth-service api --host 127.0.0.1 --port 8080 &
env -u QT_DATABASE_URL -u QT_DATABASE_URL_APP /opt/queuetruth/bin/queuetruth-service worker &
```

`queuetruth-service api` runs uvicorn with the trusted proxies of `QT_FORWARDED_ALLOW_IPS`; start the api through it,
not through `uvicorn` directly. Give each process only its own DSN, as above. Run the api and the worker as two services of your init system (for
example two systemd units with `Restart=always` and an `EnvironmentFile`). SIGTERM or SIGINT stops the worker's
loops; a job that was interrupted is requeued by the sweeper once its lease expires, and fails after
`QT_MAX_ATTEMPTS` attempts (O-Q-1, `service/tests/test_queue.py`). A local store only works when the api and every worker share the
same `QT_STORE_ROOT` directory; use S3 for more than one machine.

## Air-gapped install

- Mirror the base images the Dockerfile and compose file name (`rust:1.97-slim-bookworm`,
  `python:3.13-slim-bookworm`, `ghcr.io/astral-sh/uv:0.11.31`, `postgres:16.14-bookworm`,
  `chrislusf/seaweedfs:4.47`), build the `queuetruth` image once where the Rust and Python dependencies are
  reachable, and push it to your internal registry; set `QT_VERSION` to its tag.
- Without Docker, build the two wheels and their dependency wheels on a connected machine and install them with
  `pip install --no-index --find-links wheels/ queuetruth-service`.
- At runtime the service downloads nothing. The only outbound client in the service code is the Stripe biller,
  which is off unless `QT_BILLER=stripe`. Turn on zero-egress mode as well.

## Upgrades and migrations

Versions follow SemVer. To upgrade, read the changelog, back up, then deploy the new image or wheels and run
`queuetruth-service migrate` before starting the new api and worker (compose does this through the `migrate` service
on every `up`). Migrations are plain SQL files applied once each, in order, under an advisory lock, and recorded in
`schema_migrations` with their sha256; an applied file that changed is an error. Running `migrate` twice applies
nothing the second time (`test_admin_migrate_and_create_tenant`). Every JSON document carries `schema_version`;
`GET /v1/meta` lists the versions the service speaks (all `1` in v0.1).

Migration `0007_heartbeat_lock_timeout.sql` caps the worker heartbeat's wait for a job row at 2 s (it retries on its
next tick); likewise `POST /v1/jobs/{id}/cancel` waits at most 5 s for a row held by a worker transaction and then
answers `503` with `Retry-After: 1`, keeping no idempotency record, so retry with the same `Idempotency-Key`.

## Agents (MCP)

Point `queuetruth mcp serve` at the service with `QT_API_URL` and a `read` key in `QT_API_KEY`; add `--allow-submit`
and a `write` key only if the agent may start jobs. See [Use queuetruth from an agent](use-from-an-agent.md).

## Limits in v0.1

- No SSO or OAuth, no web UI; API keys only.
- MCP is stdio only; there is no Streamable HTTP MCP endpoint on the service.
- Job kinds: replay, calibration, scorecard, report, dataset validation, ingest and zoo. Features, hazard fitting,
  live import, why traces and the referee run locally only.
- No CLI commands for uploads, datasets, keys or retention; use the Python client or the HTTP API.
- The billing adapter is `noop` by default; nothing is metered outside your database.
- The memory cap is Linux only; the zero-egress guard covers Python-level connections in job children.
- With `QT_WORKER_CONCURRENCY=1` a worker may hold one extra picked (not started) job, a PgQueuer constraint.
- The compose stack is exercised end to end only by `scripts/compose_smoke.sh`, which needs a Docker daemon.
