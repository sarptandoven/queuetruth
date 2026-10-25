-- Zoo runs over HTTP (`/v1/zoos`): a `zoo` job kind and the `zoos` run table, shaped like `replays` and
-- `calibrations` (id, tenant, job, spec, summary). Row-level security, the policy and the grants follow migrations
-- 0001 and 0005: forced RLS keyed by the bound tenant (`qt_current_tenant()`), DML for `qt_app` and `qt_worker`, no
-- ownership for either role.
alter table jobs drop constraint jobs_kind_check;
alter table jobs add constraint jobs_kind_check
  check (kind in ('ingest', 'replay', 'calibration', 'scorecard', 'report', 'dataset_validate', 'deletion', 'zoo'));

create table zoos (
  id text primary key, tenant_id text not null references tenants(id), job_id text not null references jobs(id),
  spec jsonb not null, summary jsonb, created_at timestamptz not null default now());
create index zoos_tenant_idx on zoos (tenant_id, id);

alter table zoos enable row level security;
alter table zoos force row level security;
create policy tenant_isolation on zoos
  using (tenant_id = (select public.qt_current_tenant()))
  with check (tenant_id = (select public.qt_current_tenant()));
grant select, insert, update, delete on zoos to qt_app;
grant select, insert, update, delete on zoos to qt_worker;
