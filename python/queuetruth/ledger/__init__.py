"""Trial ledger: an append-only, hash-chained local record of every run and strategy variant tried.

Selection statistics (the Deflated Sharpe Ratio, PBO, SPA, model confidence sets) need the one input users almost
never keep: how many trials produced the result being claimed. Every `zoo`, `calibrate`, `calibrate-live`,
`fit-hazard`, `score` and `shadows sample` run (Python API or CLI) appends a record with its `run_id`, spec hash,
data hashes, code version, wall-clock times and headline metrics; `register_variant` (or `register_variants` for a
batch) records the strategy variants you try, with an optional per-period returns or per-unit scores series.
`selection_inputs(filter)` returns the trial count of one search and the series as plain data. The count is a lower
bound of what was tried: anything run with recording off, against another ledger, or outside queuetruth without
`register_variant` leaves no record.

Storage: `ledger.jsonl` in `$QT_LEDGER_DIR`, else `<project root>/.queuetruth/ledger/` (the nearest directory
upwards with `.queuetruth/`, `pyproject.toml` or `.git`), else the per-user ledger (`user_ledger()`); series live
next to it in `series/<sha256>.qts`. Each line is `{"hash": H, "record": R}` in canonical JSON, `H` = sha256 of
`R`'s bytes and `R.prev` = the previous line's `H`, so any edit, deletion, insertion or reordering is detected
(`verify_chain`); anchoring a head `seq:hash` outside the ledger also detects a rewritten history. Appends take an
exclusive file lock and fsync. `QT_LEDGER=off` disables recording; the HTTP service never records.

The ledger is a side output: it never changes a spec, a `run_id`, an output file or a summary, and an entry point
never fails because recording failed (the failure is logged as a warning).

Errors: `QT-LEDGER-001` integrity (a record altered, removed, inserted or unreadable), `QT-LEDGER-002` the ledger
cannot be written or locked, `QT-INPUT-001` no ledger at the location, `QT-CONFIG-001` an invalid record or filter.
"""

from queuetruth.ledger._capture import (
    ENV_DIR,
    ENV_STUDY,
    ENV_SWITCH,
    auto_enabled,
    code_version,
    default_ledger,
    project_root,
    record,
    recorded,
    register_variant,
    register_variants,
    run_native,
    spec_hash,
    study,
    user_ledger,
)
from queuetruth.ledger._query import (
    LEDGER_TABLE,
    as_filter,
    export,
    list_records,
    n_trials,
    selection_inputs,
    stats,
    variants,
    verify_chain,
)
from queuetruth.ledger._store import Ledger
from queuetruth.ledger.models import (
    RUN_KINDS,
    TRIAL_KINDS,
    LedgerFilter,
    LedgerRecord,
    LedgerStats,
    LedgerVerification,
    Series,
    SeriesRef,
    json_schemas,
)

__all__ = [
    "ENV_DIR",
    "ENV_STUDY",
    "ENV_SWITCH",
    "LEDGER_TABLE",
    "RUN_KINDS",
    "TRIAL_KINDS",
    "Ledger",
    "LedgerFilter",
    "LedgerRecord",
    "LedgerStats",
    "LedgerVerification",
    "Series",
    "SeriesRef",
    "as_filter",
    "auto_enabled",
    "code_version",
    "default_ledger",
    "export",
    "json_schemas",
    "list_records",
    "n_trials",
    "project_root",
    "record",
    "recorded",
    "register_variant",
    "register_variants",
    "run_native",
    "selection_inputs",
    "spec_hash",
    "stats",
    "study",
    "user_ledger",
    "variants",
    "verify_chain",
]
