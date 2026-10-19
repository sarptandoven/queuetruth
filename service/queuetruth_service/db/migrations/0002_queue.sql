-- Job lifecycle functions (SPEC 8.3). The queue payload carries only the job id, so the worker cannot set
-- app.tenant_id before claiming; claim, heartbeat, finish, sweep and mark-enqueued are SECURITY DEFINER functions
-- that return the tenant id. Fencing: heartbeat and finish only touch the row while state = 'running' and
-- attempts = the attempt the caller claimed, so a worker that lost its lease can never overwrite a later attempt.

create function qt_claim_job(p_id text, p_lease_s int)
returns table (id text, tenant_id text, kind text, spec jsonb, attempts int, resource_type text, resource_id text)
language sql security definer set search_path = pg_catalog, public as $$
  update public.jobs j set state = 'running', attempts = j.attempts + 1,
         lease_until = now() + make_interval(secs => p_lease_s),
         started_at = coalesce(j.started_at, now()),
         progress = jsonb_build_object('fraction', 0.0, 'stage', 'starting')
   where j.id = p_id and j.state = 'queued' and not j.cancel_requested
  returning j.id, j.tenant_id, j.kind, j.spec, j.attempts, j.resource_type, j.resource_id $$;

create function qt_heartbeat(p_id text, p_attempt int, p_lease_s int)
returns table (owned boolean, cancel_requested boolean)
language sql security definer set search_path = pg_catalog, public as $$
  with u as (
    update public.jobs j set lease_until = now() + make_interval(secs => p_lease_s)
     where j.id = p_id and j.state = 'running' and j.attempts = p_attempt
    returning j.cancel_requested)
  select exists (select 1 from u), coalesce((select u.cancel_requested from u), false) $$;

-- Terminal transition of a claimed attempt. Called inside the worker's tenant transaction so that usage events,
-- job files and the resource summary commit atomically with the state change.
create function qt_finish_job(p_id text, p_attempt int, p_state text, p_error jsonb, p_usage jsonb,
                              p_files_expire_at timestamptz)
returns boolean language sql security definer set search_path = pg_catalog, public as $$
  with u as (
    update public.jobs j set state = p_state, error = p_error, finished_at = now(), lease_until = null,
           usage = coalesce(p_usage, '{}'::jsonb), files_expire_at = p_files_expire_at,
           progress = jsonb_build_object('fraction', 1.0, 'stage', p_state)
     where j.id = p_id and j.state = 'running' and j.attempts = p_attempt
       and p_state in ('succeeded', 'failed', 'canceled')
    returning 1)
  select exists (select 1 from u) $$;

-- Sweeper: fails expired running jobs at max attempts, requeues the others, and returns queued jobs whose enqueue
-- was lost (enqueued_at null) or is older than p_reenqueue_after_s. The caller enqueues the returned ids and then
-- calls qt_mark_enqueued.
create function qt_sweep(p_max_attempts int, p_reenqueue_after_s int)
returns table (id text, action text)
language plpgsql security definer set search_path = pg_catalog, public as $$
begin
  return query
  with expired as (
    select j.id, j.attempts from public.jobs j
     where j.state = 'running' and j.lease_until < now()
     for update skip locked),
  failed as (
    update public.jobs j set state = 'failed', finished_at = now(), lease_until = null,
           progress = jsonb_build_object('fraction', 1.0, 'stage', 'failed'),
           error = jsonb_build_object('schema', 'qt.problem', 'schema_version', 1,
                                      'type', 'https://queuetruth.dev/problems/worker-lost',
                                      'title', 'worker lost', 'status', 500, 'code', 'QT-ENGINE-001',
                                      'detail', 'the job lease expired ' || e.attempts || ' times',
                                      'request_id', 'req_worker')
      from expired e where j.id = e.id and e.attempts >= p_max_attempts
    returning j.id),
  requeued as (
    update public.jobs j set state = 'queued', lease_until = null, enqueued_at = null,
           progress = jsonb_build_object('fraction', 0.0, 'stage', 'requeued')
      from expired e where j.id = e.id and e.attempts < p_max_attempts
    returning j.id),
  stale as (
    select j.id from public.jobs j
     where j.state = 'queued' and not j.cancel_requested
       and (j.enqueued_at is null or j.enqueued_at < now() - make_interval(secs => p_reenqueue_after_s))
       and j.id not in (select r.id from requeued r)
     for update skip locked)
  select f.id, 'failed'::text from failed f
  union all select r.id, 'requeue'::text from requeued r
  union all select s.id, 'enqueue'::text from stale s;
end $$;

create function qt_mark_enqueued(p_ids text[])
returns void language sql security definer set search_path = pg_catalog, public as $$
  update public.jobs set enqueued_at = now() where id = any(p_ids) and state = 'queued' $$;

revoke all on function qt_claim_job(text, int), qt_heartbeat(text, int, int),
  qt_finish_job(text, int, text, jsonb, jsonb, timestamptz), qt_sweep(int, int), qt_mark_enqueued(text[])
  from public;
grant execute on function qt_claim_job(text, int), qt_heartbeat(text, int, int),
  qt_finish_job(text, int, text, jsonb, jsonb, timestamptz), qt_sweep(int, int), qt_mark_enqueued(text[])
  to qt_app;
