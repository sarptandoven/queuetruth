-- PgQueuer tables carry only job ids ({"job_id": ...}); no RLS. The app role enqueues (api) and consumes (worker).
grant select, insert, update, delete on pgqueuer, pgqueuer_log, pgqueuer_statistics, pgqueuer_schedules to qt_app;
grant usage, select on all sequences in schema public to qt_app;
