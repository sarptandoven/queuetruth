13_execution_without_print

Nasdaq, audit ruling A4. Shadow bid 10 @ 100.00 behind o1 (60) and o2 (40); o3 (50) joins behind it. o1 is executed by a non-printable execution (F and C, no T), then o2 (40) and o3 (5) by printed executions. Queue ahead follows the orders, not the prints: it is 0 after o2, and the execution of o3 behind the shadow fills it 5. A print-driven simulator sees only 45 traded against 100 ahead and does not fill.
