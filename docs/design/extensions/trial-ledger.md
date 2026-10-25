# Extension: trial ledger (for SPEC 0.2, 4.3, 4.4, 7.2 and 8.8)

Status: implemented in `python/queuetruth/ledger/`, tests in `python/tests/ledger/`, user docs in
`docs/concepts/trial-ledger.md`. This page is the text to merge into the specification.

## 0.2 Error codes (two new rows)

| Code | Meaning |
|---|---|
| `QT-LEDGER-001` | trial-ledger integrity: a record or series file altered, removed, inserted, reordered or unreadable, or a file that is not a ledger; `ledger verify` failed (details list every problem by line) |
| `QT-LEDGER-002` | the trial ledger cannot be created, opened, locked (within `QT_LEDGER_LOCK_TIMEOUT_S`) or appended to |

Both exit with code 2. Neither is ever raised by a recorded entry point: a recording failure is logged on the
`queuetruth.ledger` logger at level warning and the run's own result or error is returned unchanged.

## 4.3 Python API (`queuetruth.ledger`)

```text
record(kind, *, spec=None, summary=None, error=None, options=None, started_at_ns=None, study=None, note=None,
       returns=None, scores=None, index=None, higher_is_better=True, periods_per_year=None,
       ledger=None) -> LedgerRecord | None
register_variant(name, params=None, *, returns=None, scores=None, index=None, higher_is_better=True,
                 periods_per_year=None, study=None, dataset=None, code_version=None, run_ids=(), metrics=None,
                 note=None, started_at_ns=None, ledger=None) -> LedgerRecord | None
register_variants(variants, *, ledger=None) -> list[LedgerRecord]   # one lock, one write, one fsync
list_records(filter=None, *, limit=0, ledger=None, **filters) -> list[LedgerRecord]
n_trials(filter=None, *, ledger=None, **filters) -> int
variants(filter=None, *, ledger=None, **filters) -> list[dict]
stats(filter=None, *, ledger=None, **filters) -> LedgerStats
selection_inputs(filter=None, *, series="returns", allow_mixed=False, ledger=None, **filters) -> dict
export(out, filter=None, *, selection=False, series="returns", allow_mixed=False, ledger=None,
       **filters) -> LedgerExport
verify_chain(anchor=None, *, ledger=None) -> LedgerVerification
study(name)            # context manager: the study of runs and variants recorded inside
recorded(kind)         # decorator of the entry points; run_native(cmd, runner) for the CLI's native commands
Ledger(location)       # a directory holding ledger.jsonl (and series/), or a .jsonl file
user_ledger()          # the per-user ledger (outside any project)
```

Kinds: `genesis`, `zoo`, `calibrate`, `calibrate-live`, `fit-hazard`, `score`, `shadows-sample`, `variant`.
`queuetruth.api.sample_shadows`, `run_zoo`, `calibrate`, `calibrate_live`, `fit_hazard` and `score` are wrapped by
`recorded`; a recorded call made while another recorded call runs (the zoo inside `score` and `calibrate-live`) is
part of the outer trial and not recorded. `filter` is a `LedgerFilter` (`kinds`, `study` glob, `name` glob,
`status` all|ok|error, `since_ns`, `until_ns`, `dataset`, `run_id` and `spec_hash` prefixes) or a dict of its
fields. With `QT_LEDGER=off`, `record` and `register_variant` log a warning and return None.

`selection_inputs` defaults the filter's kinds to `variant` and refuses (`QT-CONFIG-001`) a selection spanning more
than one study or kind unless `allow_mixed`. It reports `n_trials`, `n_configs`, `by_kind`, `studies`,
`linked_run_ids`, `conflicts`, `aligned` and the common `index`, and per trial its series with `index` and
`dataset_hash`.

## 4.4 CLI

`queuetruth ledger list | stats | export | verify | add` (documented in docs/concepts/trial-ledger.md). The command
is dispatched in Python before the clap parse (`queuetruth.ledger.cli.main_if_ledger`), which also answers
`queuetruth help ledger [COMMAND]`; adding a `ledger` entry to `crates/cli/src/grammar.rs` (outside this
workstream's paths) would list it in `queuetruth --help`, shell completion and docs/reference/cli.md. Exit codes as
0.2; `ledger verify` on a failing ledger is `QT-LEDGER-001`, exit 2, with the `qt.ledger_verification` document in
the error's `details.verification`.

## Record and file format

`ledger.jsonl`, one line per record: the RFC 8785 canonical JSON of `{"hash": H, "record": R}` followed by `\n`,
where `H` = lower-case hex sha256 of the exact UTF-8 bytes of `R` as they appear in the line and `R.prev` = `H` of
the previous line (64 zeros for the genesis record). `R` is a `qt.ledger_record` (schema_version 1, unknown fields
rejected): `ledger_id` (32 hex, fixed by genesis), `seq` (0 for genesis, then +1), `prev`, `kind`, `status`
(ok|error), `study`, `trial_id`, `run_id`, `spec_hash`, `dataset_hash`, `inputs`, `name`, `params`, `metrics`,
`series` (`kind` returns|scores, `sha256`, `n_obs` 1 to 1,000,000, `indexed`, `periods_per_year`,
`higher_is_better`), `run_ids`, `code` (`queuetruth`, `engine`, `core`, `git`, `project_git`, `user`), `error`
(`code`, `message`), `note`, `started_at_ns`, `recorded_at_ns`.

Series files: `<ledger dir>/series/<sha256>.qts`, named by the sha256 of their bytes: `b"QTS1"`, u64 n, u8 indexed,
n little-endian f64 values, then (when indexed) n little-endian i64 period ends in UTC ns, strictly increasing.
Counting and listing never read them; `selection_inputs`, exports and `verify` check each against its sha256.

Identities:

- `run_id` = the summary's `run_id` (fit-hazard: the suffix of `model_id`); without one (`shadows sample`) it is
  recomputed from the summary's spec echo by the Rust core (`queuetruth.api.run_id`), null when that fails.
  `calibrate-live` echoes `calibration_source`, which is not a spec field and is left out.
- `spec_hash` = first 16 hex of sha256 of canonical `{"kind": kind, "spec": S}`, S = the spec echo (for a failed
  run: the spec as given, validated and defaulted when it validates, keyword options that are spec fields folded
  in) with `EXECUTION_ONLY` keys removed, `V1_1_DEFAULTS` dropped and every reference replaced by `"$ref"`.
- `inputs` = {field path: identity} of every reference of the echo, resolved as that kind's `run_id` resolves it
  (SPEC 7.3): the Rust rule (crates/calib/src/refs.rs) for `zoo`, `calibrate` (not latency) and `shadows-sample`,
  `queuetruth.score._io.resolve_ref` for the others; `dataset_hash` = sha256 of canonical `inputs`. Failed runs have
  neither. A variant's `inputs` is `{"dataset": identity}` for a path (`os.PathLike` or `@PATH`) or
  `{"dataset_label": label}`.
- `trial_id` = `run_id` for a run (null for a failed run, and for a result the CLI refuses: `score` with
  `n_matched` 0, a calibration without events or estimate, recorded as `error` with `QT-INPUT-002`); for a variant,
  first 16 hex of sha256 of canonical `{"name", "params", "dataset_hash", "code", "outcome"}`, `outcome` =
  `{"series": SeriesRef}` or, without a series, `{"metrics": metrics}`.
- Trial count: distinct `trial_id`s, where a trial whose records carry k > 1 distinct series counts k times and a
  run whose `run_id` a selected variant lists in `run_ids` is not counted on its own.

## 7.2 Determinism

The ledger is excluded from every determinism hash: it holds wall-clock values and lives outside the output
directories. Recording reads the spec and the summary after the run and never changes the spec, the outputs, the
summary or `run_id`; `python/tests/ledger/test_ledger_e2e.py` runs shadows, zoo, calibrate, features, fit-hazard,
score and calibrate-live with the ledger on and with `QT_LEDGER=off` and asserts byte-identical outputs and
summaries (JSON without `timings`).

## Location, switches and the service (8.8)

`$QT_LEDGER_DIR`, else `<project root>/.queuetruth/ledger/` (project root: the nearest directory upwards from the
working directory with `.queuetruth/`, `pyproject.toml` or `.git`), created with a `.gitignore` of `*`, else the
per-user ledger `<user data dir>/queuetruth/ledger` (never the bare working directory). A ledger that would lie inside
a directory the run reads is replaced by the per-user ledger with a warning (not recorded when that lies inside an
input too), because a directory input without a manifest is identified by every file under it.
`QT_LEDGER=off|0|false|no|disabled` records nothing; `on|1|true|yes|enabled` forces recording. A process that
imported `queuetruth_service` never records automatically, whatever the switch: service jobs run in a child
interpreter whose environment is rebuilt from `CHILD_ENV_ALLOW`, and one local file would mix tenants. Under pytest
(`PYTEST_CURRENT_TEST`) automatic recording needs `QT_LEDGER_DIR` or `QT_LEDGER=on`. `QT_LEDGER_STUDY` names the
study of automatic records. `QT_LEDGER_LOCK_TIMEOUT_S` (default 30) bounds the wait for the lock.

## Concurrency and recovery

Appends hold an exclusive lock on `ledger.jsonl.lock` (`flock`; `msvcrt.locking` on Windows; plus a process-wide
thread lock), read the last committed line, write the new line with one `write` call and `fsync`. Bytes after the
last newline are uncommitted: reads ignore them; the next append commits them when they are a complete record that
extends the chain, else moves them to `torn/<offset>-<sha12>.bin` and truncates. An unreadable last committed line
stops appends with `QT-LEDGER-001`, as does a file whose first line is not a ledger line (it is never overwritten).
`Ledger.append` refuses a `genesis` record and re-validates every record. A forked child replaces the in-process
lock (`os.register_at_fork`).

## Open points for the integrator

- Done at integration: the ledger documents (`queuetruth.ledger.models.DOCUMENTS`, the six `qt.ledger_*` and
  `qt.selection_inputs`) and the `qt.ledger` Arrow layout are registered and ship in `schemas/json`,
  `schemas/arrow` and the core's embedded copies.
- The rest of the test suite does not record: automatic recording is off under pytest unless `QT_LEDGER_DIR` or
  `QT_LEDGER=on` is set. Setting `QT_LEDGER=off` in `python/tests/conftest.py` and `service/tests/conftest.py` as
  well would make that explicit.
- Optionally set `QT_LEDGER=off` in the service's `child_env` (service/queuetruth_service/executor.py) so that the
  service's off state does not rest on the `sys.modules` check alone.
- Optionally make `queuetruth.score._io.resolve_ref` skip `.queuetruth/` when hashing a manifest-less directory.
- Add `ledger` to `crates/cli/src/grammar.rs` (Python implementation, trailing passthrough arguments), rebuild, and
  regenerate docs/reference so it is listed in `queuetruth --help`.
- Add `docs/concepts/trial-ledger.md` to the mkdocs nav; register the ledger error codes in spec 0.2.
