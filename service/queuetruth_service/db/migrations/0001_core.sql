-- queuetruth service schema (SPEC 8.2). Applied once by queuetruth_service.db.runner as the owner role, which must be
-- a superuser or have BYPASSRLS (FORCE ROW LEVEL SECURITY also applies to table owners; the SECURITY DEFINER
-- functions below run as the owner). The qt_app password is set by the runner from QT_DATABASE_URL_APP, never here.

do $$ begin
  create role qt_app login nosuperuser nobypassrls nocreatedb nocreaterole;
exception when duplicate_object then null; end $$;

create table tenants (
  id text primary key,
  name text not null,
  plan text not null default 'dev',
  zero_egress boolean not null default false,
  retention_uploads_days int not null default 30 check (retention_uploads_days between 1 and 365),
  retention_outputs_days int not null default 30 check (retention_outputs_days between 1 and 365),
  stripe_customer_id text,
  created_at timestamptz not null default now()
);

create table api_keys (
  id text primary key,
  tenant_id text not null references tenants(id),
  prefix text not null unique,
  key_hash text not null,
  scopes text[] not null,
  name text not null default '',
  created_at timestamptz not null default now(),
  last_used_at timestamptz,
  revoked_at timestamptz
);
create index api_keys_tenant_idx on api_keys (tenant_id, id);

create table idempotency_keys (
  tenant_id text not null references tenants(id),
  key text not null,
  method text not null,
  path text not null,
  request_hash text not null,
  state text not null check (state in ('in_progress', 'complete')),
  response_status int,
  response_body jsonb,
  response_headers jsonb,
  created_at timestamptz not null default now(),
  locked_at timestamptz not null default now(),
  primary key (tenant_id, key)
);

create table rate_limit_buckets (
  tenant_id text not null references tenants(id),
  key_id text not null,
  bucket text not null,
  tokens double precision not null,
  updated_at timestamptz not null,
  primary key (key_id, bucket)
);

create table uploads (
  id text primary key,
  tenant_id text not null references tenants(id),
  purpose text not null,
  filename text not null,
  content_type text,
  size_bytes bigint not null,
  sha256 text not null,
  status text not null check (status in ('pending', 'complete', 'verified', 'failed')),
  object_key text not null,
  multipart_id text,
  part_size bigint not null,
  created_at timestamptz not null default now(),
  completed_at timestamptz
);
create index uploads_tenant_idx on uploads (tenant_id, id);

create table datasets (
  id text primary key,
  tenant_id text not null references tenants(id),
  name text not null,
  kind text not null,
  format text not null,
  venue text not null,
  status text not null check (status in ('pending', 'validating', 'ready', 'failed', 'deleted')),
  upload_ids text[] not null,
  ingest jsonb not null default '{}',
  manifest jsonb,
  data_quality jsonb,
  object_prefix text not null,
  job_id text,
  created_at timestamptz not null default now()
);
create index datasets_tenant_idx on datasets (tenant_id, id);

create table jobs (
  id text primary key,
  tenant_id text not null references tenants(id),
  kind text not null check (kind in ('ingest', 'replay', 'calibration', 'scorecard', 'report', 'dataset_validate',
                                     'deletion')),
  state text not null default 'queued'
    check (state in ('queued', 'running', 'succeeded', 'failed', 'canceled', 'expired')),
  spec jsonb not null default '{}',
  resource_type text not null,
  resource_id text not null,
  progress jsonb not null default '{"fraction": 0.0, "stage": "queued"}',
  error jsonb,
  usage jsonb not null default '{}',
  attempts int not null default 0,
  lease_until timestamptz,
  enqueued_at timestamptz,
  cancel_requested boolean not null default false,
  created_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz,
  files_expire_at timestamptz,
  run_id text
);
create index jobs_tenant_idx on jobs (tenant_id, id);
create index jobs_sweep_idx on jobs (state, lease_until) where state in ('queued', 'running');

create table job_files (
  job_id text not null references jobs(id),
  tenant_id text not null references tenants(id),
  name text not null,
  bytes bigint not null,
  sha256 text not null,
  content_type text not null,
  object_key text not null,
  primary key (job_id, name)
);

create table replays (
  id text primary key, tenant_id text not null references tenants(id), job_id text not null references jobs(id),
  spec jsonb not null, summary jsonb, created_at timestamptz not null default now());
create table calibrations (
  id text primary key, tenant_id text not null references tenants(id), job_id text not null references jobs(id),
  spec jsonb not null, summary jsonb, created_at timestamptz not null default now());
create table scorecards (
  id text primary key, tenant_id text not null references tenants(id), job_id text not null references jobs(id),
  spec jsonb not null, summary jsonb, created_at timestamptz not null default now());
create table reports (
  id text primary key, tenant_id text not null references tenants(id), job_id text not null references jobs(id),
  spec jsonb not null, summary jsonb, created_at timestamptz not null default now());

create table usage_events (
  id text primary key,
  tenant_id text not null references tenants(id),
  job_id text,
  meter text not null,
  quantity numeric not null,
  unit text not null,
  identifier text not null unique,
  created_at timestamptz not null default now(),
  pushed_at timestamptz,
  push_error text
);
create index usage_events_unpushed_idx on usage_events (tenant_id, created_at) where pushed_at is null;

create table tombstones (
  id text primary key,
  tenant_id text not null references tenants(id),
  resource_type text not null,
  resource_id text not null,
  object_count int not null,
  sha256s text[] not null,
  deleted_at timestamptz not null default now()
);

create table audit_log (
  id text primary key,
  tenant_id text not null references tenants(id),
  actor_key_id text,
  action text not null,
  resource_type text not null,
  resource_id text not null,
  at timestamptz not null default now()
);

-- Row-level security: every tenant table is visible only inside a transaction that ran
-- SELECT set_config('app.tenant_id', <tenant>, true).
alter table tenants enable row level security;
alter table tenants force row level security;
create policy tenant_isolation on tenants
  using (id = current_setting('app.tenant_id', true))
  with check (id = current_setting('app.tenant_id', true));

do $$ declare t text; begin
  foreach t in array array['api_keys', 'idempotency_keys', 'rate_limit_buckets', 'uploads', 'datasets', 'jobs',
                           'job_files', 'replays', 'calibrations', 'scorecards', 'reports', 'usage_events',
                           'tombstones', 'audit_log'] loop
    execute format('alter table %I enable row level security', t);
    execute format('alter table %I force row level security', t);
    execute format($p$create policy tenant_isolation on %I
                      using (tenant_id = current_setting('app.tenant_id', true))
                      with check (tenant_id = current_setting('app.tenant_id', true))$p$, t);
    execute format('grant select, insert, update, delete on %I to qt_app', t);
  end loop;
end $$;
grant select, update on tenants to qt_app;

-- Pre-auth key lookup: the only cross-tenant read the API can do.
create function auth_lookup(p_prefix text)
returns table (key_id text, tenant_id text, key_hash text, scopes text[], revoked_at timestamptz)
language sql stable security definer set search_path = pg_catalog, public as $$
  select k.id, k.tenant_id, k.key_hash, k.scopes, k.revoked_at from public.api_keys k where k.prefix = p_prefix $$;

-- Tenant ids for the worker's per-tenant maintenance loops (retention, metering push, upload verification).
create function qt_tenant_ids()
returns table (id text)
language sql stable security definer set search_path = pg_catalog, public as $$
  select t.id from public.tenants t order by t.id $$;

-- Atomic token bucket (SPEC 8.7); runs inside the request's tenant transaction, so RLS applies.
create function qt_take_token(p_tenant text, p_key text, p_bucket text, p_rate_per_s float8, p_burst float8)
returns table (allowed boolean, remaining float8, retry_after_s float8)
language plpgsql as $$
declare cur float8; ts timestamptz; nowts timestamptz := clock_timestamp(); refill float8;
begin
  insert into rate_limit_buckets as b (tenant_id, key_id, bucket, tokens, updated_at)
  values (p_tenant, p_key, p_bucket, p_burst, nowts) on conflict (key_id, bucket) do nothing;
  select b.tokens, b.updated_at into cur, ts from rate_limit_buckets b
   where b.key_id = p_key and b.bucket = p_bucket for update;
  refill := least(p_burst, cur + greatest(0, extract(epoch from nowts - ts)) * p_rate_per_s);
  if refill >= 1 then
    update rate_limit_buckets set tokens = refill - 1, updated_at = nowts where key_id = p_key and bucket = p_bucket;
    return query select true, refill - 1, 0::float8;
  else
    update rate_limit_buckets set tokens = refill, updated_at = nowts where key_id = p_key and bucket = p_bucket;
    return query select false, refill, (1 - refill) / p_rate_per_s;
  end if;
end $$;

revoke all on function auth_lookup(text), qt_tenant_ids() from public;
grant execute on function auth_lookup(text), qt_tenant_ids(), qt_take_token(text, text, text, float8, float8)
  to qt_app;
