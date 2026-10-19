-- The heartbeat waits at most 2 s for the job row (a function-level setting, in force only while the function runs).
-- A heartbeat queued behind a long holder would otherwise wait indefinitely and, queued itself, make later lockers of
-- the row (a cancel) wait behind it too; with the timeout it fails with `lock_not_available` and the worker retries on
-- its next tick, well inside the lease (LEASE_S >> 2 s).
alter function qt_heartbeat(text, int, int) set lock_timeout = '2s';
