"""The operations, one module each (plan section 5.1): `ingest`, `replay`, `shadows`, `features`, `fixtures`,
`referee`, `zoo`, `calibrate`, `fit_hazard`, `live`, `score`, `report`, `dq`, `why`, `export`, plus `run_id`, `tables`
and `session`. Shared plumbing is in `_call`; the operation table (`OperationDef`, one per operation, declared in its
module as `OPERATIONS`) is in `table`, and `queuetruth.runner.run` runs any of them by name.

Every operation takes a spec model or a plain dict (validated; an invalid spec raises `QtError("QT-CONFIG-001")`) and
returns the Pydantic summary model. Native work runs in `queuetruth._core` with the GIL released; its
`QtCoreError(code, message)` is re-raised as `QtError`. `queuetruth.api` re-exports every public name, so `qt.<name>`
and `queuetruth.api.<name>` keep working; import from there. This package imports none of its modules, so importing
one operation does not load the others.
"""
