-- Security hardening (independent security review: H1, M1, M3, M4). Applied by the owner role like 0001-0004.
--
-- Roles. `qt_app` (api) and `qt_worker` (worker) are login roles that are not superusers, have no BYPASSRLS and own
-- nothing; the api and the worker refuse to start with any other kind of role (queuetruth_service.db.pool). The api
-- role can only act for a tenant whose API key it presents; the worker role claims jobs and runs maintenance.
--
-- Tenant binding. RLS used to trust `app.tenant_id`, a setting every session may change, so one leaked `qt_app`
-- password was every tenant's data. Now the tenant is bound only by SECURITY DEFINER functions that check ownership:
--   qt_authenticate(prefix, key_hash)  api: an unrevoked key whose HMAC(pepper, key) matches (needs the key itself);
--   qt_bind_job(job_id, attempt)       worker: a job this attempt holds (state running, fenced by attempt);
--   qt_bind_tenant(tenant_id)          worker maintenance passes only.
-- A binding sets `app.tenant_id` plus `app.tenant_sig` = MAC(k2, MAC(k1, tenant | backend pid | transaction start))
-- for the current transaction; k1/k2 live in `qt_binding_key`, which no login role can read. `qt_current_tenant()`
-- returns the tenant only while the MAC verifies, and every policy compares `tenant_id` with it. Setting the GUCs by
-- hand, or replaying a signature in another transaction or session, binds nothing.

do $$ begin
  create role qt_worker login nosuperuser nobypassrls nocreatedb nocreaterole noreplication;
exception when duplicate_object then null; end $$;

do $$ begin
  if exists (select 1 from pg_roles where rolname in ('qt_app', 'qt_worker') and (rolsuper or rolbypassrls)) then
    raise exception 'qt_app and qt_worker must not be superusers or have BYPASSRLS';
  end if;
end $$;

revoke create on schema public from public;

-- ---- binding secret and functions ---------------------------------------------------------------------------------

create table qt_binding_key (
  id int primary key check (id = 1),
  k1 bytea not null,
  k2 bytea not null
);
alter table qt_binding_key enable row level security;
alter table qt_binding_key force row level security;  -- no policy: invisible to every role without BYPASSRLS
revoke all on qt_binding_key from public;
insert into qt_binding_key (id, k1, k2) values (
  1,
  decode(replace(gen_random_uuid()::text || gen_random_uuid()::text || gen_random_uuid()::text, '-', ''), 'hex'),
  decode(replace(gen_random_uuid()::text || gen_random_uuid()::text || gen_random_uuid()::text, '-', ''), 'hex'));

create function qt_binding_mac(p_tenant text) returns text
language sql stable security definer set search_path = pg_catalog, pg_temp as $$
  select encode(sha256(k.k2 || sha256(k.k1 || convert_to(
           p_tenant || '|' || pg_backend_pid()::text || '|'
           || ((extract(epoch from transaction_timestamp()) * 1000000)::bigint)::text, 'UTF8'))), 'hex')
    from public.qt_binding_key k where k.id = 1 $$;

create function qt_current_tenant() returns text
language plpgsql stable security definer set search_path = pg_catalog, pg_temp as $$
declare
  t text := nullif(current_setting('app.tenant_id', true), '');
  s text := nullif(current_setting('app.tenant_sig', true), '');
begin
  if t is null or s is null then
    return null;
  end if;
  -- compare digests, not the strings, so the comparison time says nothing about the expected value
  if sha256(convert_to(s, 'UTF8')) <> sha256(convert_to(coalesce(public.qt_binding_mac(t), ''), 'UTF8')) then
    return null;
  end if;
  return t;
end $$;

create function qt_bind(p_tenant text) returns text
language plpgsql volatile security definer set search_path = pg_catalog, pg_temp as $$
begin
  perform set_config('app.tenant_id', p_tenant, true);
  perform set_config('app.tenant_sig', public.qt_binding_mac(p_tenant), true);
  return p_tenant;
end $$;

create function qt_authenticate(p_prefix text, p_key_hash text)
returns table (key_id text, tenant_id text, scopes text[])
language plpgsql volatile security definer set search_path = pg_catalog, pg_temp as $$
#variable_conflict use_column
declare r record;
begin
  select k.id, k.tenant_id, k.scopes, k.key_hash into r
    from public.api_keys k where k.prefix = p_prefix and k.revoked_at is null;
  if not found
     or sha256(convert_to(r.key_hash, 'UTF8')) <> sha256(convert_to(coalesce(p_key_hash, ''), 'UTF8')) then
    return;
  end if;
  perform public.qt_bind(r.tenant_id);
  key_id := r.id;
  tenant_id := r.tenant_id;
  scopes := r.scopes;
  return next;
end $$;

create function qt_bind_job(p_job_id text, p_attempt int) returns text
language plpgsql volatile security definer set search_path = pg_catalog, pg_temp as $$
declare t text;
begin
  select j.tenant_id into t from public.jobs j
   where j.id = p_job_id and j.state = 'running' and j.attempts = p_attempt;
  if t is null then
    return null;
  end if;
  return public.qt_bind(t);
end $$;

create function qt_bind_tenant(p_tenant text) returns text
language plpgsql volatile security definer set search_path = pg_catalog, pg_temp as $$
begin
  if not exists (select 1 from public.tenants t where t.id = p_tenant) then
    return null;
  end if;
  return public.qt_bind(p_tenant);
end $$;

-- the old pre-auth lookup returned key hashes to any qt_app session
drop function auth_lookup(text);

-- ---- policies -----------------------------------------------------------------------------------------------------

alter policy tenant_isolation on tenants
  using (id = (select public.qt_current_tenant()))
  with check (id = (select public.qt_current_tenant()));

do $$ declare t text; begin
  foreach t in array array['api_keys', 'idempotency_keys', 'rate_limit_buckets', 'uploads', 'datasets', 'jobs',
                           'job_files', 'replays', 'calibrations', 'scorecards', 'reports', 'usage_events',
                           'tombstones', 'audit_log'] loop
    execute format($p$alter policy tenant_isolation on %I
                      using (tenant_id = (select public.qt_current_tenant()))
                      with check (tenant_id = (select public.qt_current_tenant()))$p$, t);
    execute format('grant select, insert, update, delete on %I to qt_worker', t);
  end loop;
end $$;
grant select, update on tenants to qt_worker;

-- ---- per-tenant rate limits (M4: buckets were per API key, so minting keys multiplied the budget) -----------------

drop function qt_take_token(text, text, text, float8, float8);
delete from rate_limit_buckets;
alter table rate_limit_buckets drop constraint rate_limit_buckets_pkey;
alter table rate_limit_buckets drop column key_id;
alter table rate_limit_buckets add primary key (tenant_id, bucket);

-- Runs as the caller inside its bound transaction (RLS applies); the tenant is the bound one, never an argument.
create function qt_take_token(p_bucket text, p_rate_per_s float8, p_burst float8)
returns table (allowed boolean, remaining float8, retry_after_s float8)
language plpgsql volatile set search_path = pg_catalog, pg_temp as $$
declare
  t text := public.qt_current_tenant();
  cur float8;
  ts timestamptz;
  nowts timestamptz := clock_timestamp();
  refill float8;
begin
  if t is null then
    raise exception 'qt_take_token: no tenant bound' using errcode = '42501';
  end if;
  insert into public.rate_limit_buckets as b (tenant_id, bucket, tokens, updated_at)
  values (t, p_bucket, p_burst, nowts) on conflict (tenant_id, bucket) do nothing;
  select b.tokens, b.updated_at into cur, ts from public.rate_limit_buckets b
   where b.tenant_id = t and b.bucket = p_bucket for update;
  refill := least(p_burst, cur + greatest(0, extract(epoch from nowts - ts)) * p_rate_per_s);
  if refill >= 1 then
    update public.rate_limit_buckets b set tokens = refill - 1, updated_at = nowts
     where b.tenant_id = t and b.bucket = p_bucket;
    return query select true, refill - 1, 0::float8;
  else
    update public.rate_limit_buckets b set tokens = refill, updated_at = nowts
     where b.tenant_id = t and b.bucket = p_bucket;
    return query select false, refill, (1 - refill) / p_rate_per_s;
  end if;
end $$;

-- ---- job lifecycle: per-tenant running cap (M4) and tenant-checked finish (M3) ------------------------------------

drop function qt_claim_job(text, int);

-- Claims a queued job unless its tenant already runs `p_tenant_max_running` jobs with a live lease (0 = no cap); a
-- capped claim returns one row with `deferred` true and changes nothing (the worker re-enqueues it with a delay).
-- Claims of one tenant are serialized by an advisory lock so concurrent workers cannot overshoot the cap.
create function qt_claim_job(p_id text, p_lease_s int, p_tenant_max_running int)
returns table (id text, tenant_id text, kind text, spec jsonb, attempts int, resource_type text, resource_id text,
               deferred boolean)
language plpgsql volatile security definer set search_path = pg_catalog, pg_temp as $$
#variable_conflict use_column
declare t text;
begin
  select j.tenant_id into t from public.jobs j
   where j.id = p_id and j.state = 'queued' and not j.cancel_requested;
  if t is null then
    return;
  end if;
  if p_tenant_max_running > 0 then
    perform pg_advisory_xact_lock(727275, hashtext(t));
    if (select count(*) from public.jobs r
         where r.tenant_id = t and r.state = 'running' and r.lease_until > now()) >= p_tenant_max_running then
      return query select p_id, t, null::text, null::jsonb, null::int, null::text, null::text, true;
      return;
    end if;
  end if;
  return query
    update public.jobs j set state = 'running', attempts = j.attempts + 1,
           lease_until = now() + make_interval(secs => p_lease_s),
           started_at = coalesce(j.started_at, now()),
           progress = jsonb_build_object('fraction', 0.0, 'stage', 'starting')
     where j.id = p_id and j.state = 'queued' and not j.cancel_requested
    returning j.id, j.tenant_id, j.kind, j.spec, j.attempts, j.resource_type, j.resource_id, false;
end $$;

-- Terminal transition, only for the tenant the calling transaction is bound to (qt_bind_job).
create or replace function qt_finish_job(p_id text, p_attempt int, p_state text, p_error jsonb, p_usage jsonb,
                                         p_files_expire_at timestamptz)
returns boolean language sql security definer set search_path = pg_catalog, pg_temp as $$
  with u as (
    update public.jobs j set state = p_state, error = p_error, finished_at = now(), lease_until = null,
           usage = coalesce(p_usage, '{}'::jsonb), files_expire_at = p_files_expire_at,
           progress = jsonb_build_object('fraction', 1.0, 'stage', p_state)
     where j.id = p_id and j.state = 'running' and j.attempts = p_attempt
       and j.tenant_id = public.qt_current_tenant()
       and p_state in ('succeeded', 'failed', 'canceled')
    returning 1)
  select exists (select 1 from u) $$;

-- ---- measured upload bytes (M1: billing used the declared size) --------------------------------------------------

alter table uploads add column stored_bytes bigint;

-- ---- grants -------------------------------------------------------------------------------------------------------

revoke all on function qt_binding_mac(text), qt_current_tenant(), qt_bind(text), qt_authenticate(text, text),
  qt_bind_job(text, int), qt_bind_tenant(text), qt_take_token(text, float8, float8),
  qt_claim_job(text, int, int) from public;

grant execute on function qt_current_tenant(), qt_take_token(text, float8, float8) to qt_app, qt_worker;
grant execute on function qt_authenticate(text, text) to qt_app;

revoke execute on function qt_tenant_ids(), qt_heartbeat(text, int, int),
  qt_finish_job(text, int, text, jsonb, jsonb, timestamptz), qt_sweep(int, int), qt_mark_enqueued(text[])
  from qt_app;
grant execute on function qt_bind_job(text, int), qt_bind_tenant(text), qt_tenant_ids(), qt_claim_job(text, int, int),
  qt_heartbeat(text, int, int), qt_finish_job(text, int, text, jsonb, jsonb, timestamptz), qt_sweep(int, int),
  qt_mark_enqueued(text[])
  to qt_worker;

-- The api only enqueues (insert, plus the log row PgQueuer writes) and counts the queue for /v1/health.
revoke update, delete on pgqueuer, pgqueuer_log from qt_app;
revoke all on pgqueuer_statistics, pgqueuer_schedules from qt_app;
grant select, insert, update, delete on pgqueuer, pgqueuer_log, pgqueuer_statistics, pgqueuer_schedules to qt_worker;
grant usage, select on all sequences in schema public to qt_worker;
