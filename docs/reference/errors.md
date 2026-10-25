# Error codes

Every error carries a stable code `QT-<AREA>-<NNN>` (defined in `qt_types::QtError` in Rust and
`queuetruth.errors.QtError` in Python). Messages name the file, row, byte offset or spec field at fault.

## Codes

| Code | Meaning | Exit code | Typical cause and fix |
|---|---|---|---|
| <a id="qt-usage-001"></a>`QT-USAGE-001` | Bad command-line usage or an invalid combination of flags | 2 | A missing required flag, two mutually exclusive flags, or a Python-only command run in `queuetruth-core`. Run the command with `--help`. |
| <a id="qt-config-001"></a>`QT-CONFIG-001` | Invalid spec or configuration value (the field is named) | 2 | An unknown spec field, a value out of range, a pro-rata venue, an exact replay on an L2-only dataset. Fix the named field. |
| <a id="qt-input-001"></a>`QT-INPUT-001` | Input file missing or unreadable | 2 | A wrong path, or a dataset directory without `manifest.json`. |
| <a id="qt-input-002"></a>`QT-INPUT-002` | Input format or schema mismatch (file, row or byte offset named) | 2 | A Parquet file with the wrong `qt.schema` metadata or columns, a truncated ITCH or DBN file, an invalid live log, a scorecard with no scored order ("NOT SCORED", with the count per reason), a model file whose sha256 does not match, or a crafted or damaged `model.npz`. |
| <a id="qt-input-003"></a>`QT-INPUT-003` | Duplicate id (`shadow_id`, `order_key`, `fill_id`) | 2 | Two shadows with the same id, or a live log with repeated fills after dedupe. |
| <a id="qt-input-004"></a>`QT-INPUT-004` | Unit conversion not exact | 2 | A decimal price or size that cannot be represented at the instrument's `price_exp` or `size_exp`. Fix the instrument map. |
| <a id="qt-data-001"></a>`QT-DATA-001` | Data-quality verdict `unusable` (with `--strict`), or a calibration without usable data | 2 | `MAYBE_BAD_BOOK` records, too many crossed books or unknown references (see [data quality](../concepts/exact-truth.md#data-quality)); for `calibrate` and `calibrate-live`, window dates missing from the dataset, an empty train or test window, or no filled train order. |
| <a id="qt-data-002"></a>`QT-DATA-002` | `--strict` and a non-zero invariant or anomaly counter | 2 | Read `invariants` and `data_quality.reasons` in the summary. |
| <a id="qt-engine-001"></a>`QT-ENGINE-001` | Internal invariant broken (a bug); includes instrument and sequence | 2 | Please report it with the command and the run id. |
| <a id="qt-remote-001"></a>`QT-REMOTE-001` | The HTTP API returned an error; the problem's own `code` is echoed | 2 | See the problem type below. |
| <a id="qt-calib-001"></a>`QT-CALIB-001` | `export hftbacktest` or `export nautilus` refuses a calibration | 1 | The fit has too few events, a refusing trust flag (`at_grid_edge`, `flat_likelihood`, `not_identified`, `degenerate_ci`, `own_orders_in_depth`) or does not beat the library default out of sample. The message lists the reasons; fit on more data or a wider grid, or pass `--force` to write the snippet under a NOT VALIDATED header. See [Calibration](../concepts/calibration.md#trust-verdict). |
| <a id="qt-gate-001"></a>`QT-GATE-001` | A requested gate failed | 1 | `--strict`, `--fail-abs-bias`, `--fail-not-equivalent`, a referee tolerance, a scorecard blocked because your own orders could not be removed from L2 depth (`own_orders_in_l2_depth`), or `doctor` problems. The command itself completed and wrote its outputs. |
| <a id="qt-ledger-001"></a>`QT-LEDGER-001` | Trial-ledger integrity: a record or series file altered, removed, inserted, reordered or unreadable, or a file that is not a ledger | 2 | `ledger verify` lists every problem by line. Restore the ledger from a copy, or start a new one with `QT_LEDGER_DIR`; see [Trial ledger](../concepts/trial-ledger.md). |
| <a id="qt-ledger-002"></a>`QT-LEDGER-002` | The trial ledger cannot be created, opened, locked (within `QT_LEDGER_LOCK_TIMEOUT_S`) or appended to | 2 | A read-only or full disk, or another process holding the lock. Set `QT_LEDGER_DIR` to a writable directory, or `QT_LEDGER=off` to stop recording. |

## Exit codes

`0` success; `1` the command completed but a gate you asked for failed, or an export was refused (`QT-CALIB-001`); `2` any error (usage, config, input, data,
remote, internal). Use them in CI: `1` means "the simulator drifted", `2` means "the check could not run".

## The `qt.error` document

With `--format json`, a failing command prints exactly one JSON document to stdout:

```json
{"schema": "qt.error", "schema_version": 1, "code": "QT-INPUT-001", "message": "...", "hint": null, "details": {}}
```

In Python every code raises `queuetruth.QtError` with `.code`, `.message`, `.hint`, `.details` and `.exit_code`;
`str(e)` is `"<code>: <message>"`. HTTP errors raise `queuetruth.QtApiError` (code `QT-REMOTE-001`) with `.problem`
(the `qt.problem` document), `.status` and `.remote_code`.

## Data-quality verdicts and `--strict`

Every summary repeats the dataset's data-quality verdict (`clean`, `warnings`, `unusable`). Without `--strict` an
`unusable` dataset is still processed and the verdict travels with every result; with `--strict` it is an error
(`QT-DATA-001`), and any non-zero invariant or anomaly counter is `QT-DATA-002`.

## HTTP problem types

The service answers errors with RFC 9457 `application/problem+json` documents whose `type` is
`https://queuetruth.dev/problems/<slug>`. The table lists every slug the v0.1 service emits (from
`service/queuetruth_service`), with the `code` it carries.

| Status | Slug | Code | When |
|---|---|---|---|
| 401 | <a id="problem-unauthorized"></a>`unauthorized` | `QT-USAGE-001` | Missing or invalid API key. |
| 403 | <a id="problem-forbidden"></a>`forbidden` | `QT-USAGE-001` | The key lacks the scope, or a signed store URL is invalid or expired. |
| 404 | <a id="problem-not-found"></a>`not-found` | `QT-USAGE-001` | Unknown id, or another tenant's id (never 403). |
| 405 | <a id="problem-method-not-allowed"></a>`method-not-allowed` | `QT-USAGE-001` | Wrong HTTP method for the path. |
| 409 | <a id="problem-idempotency-in-flight"></a>`idempotency-in-flight` | `QT-USAGE-001` | A request with the same `Idempotency-Key` is still being processed. |
| 409 | <a id="problem-job-terminal"></a>`job-terminal` | `QT-USAGE-001` | Cancelling a job that already finished. |
| 409 | <a id="problem-job-not-succeeded"></a>`job-not-succeeded` | `QT-USAGE-001` | Listing files of, or referencing, a job that has not succeeded. |
| 409 | <a id="problem-dataset-not-ready"></a>`dataset-not-ready` | `QT-USAGE-001` | A spec references a dataset that is still validating or failed. |
| 409 | <a id="problem-upload-not-pending"></a>`upload-not-pending` | `QT-USAGE-001` | Completing an upload that is not pending. |
| 409 | <a id="problem-upload-not-verified"></a>`upload-not-verified` | `QT-USAGE-001` | A spec references an upload whose sha256 is not verified yet. |
| 410 | <a id="problem-gone"></a>`gone` | `QT-USAGE-001` | The job's files expired under the retention policy. |
| 413 | <a id="problem-payload-too-large"></a>`payload-too-large` | `QT-USAGE-001` | A JSON body over 1 MiB, or an upload over the size limit. |
| 416 | <a id="problem-range-not-satisfiable"></a>`range-not-satisfiable` | `QT-USAGE-001` | A ranged download outside the object. |
| 422 | <a id="problem-validation-error"></a>`validation-error` | `QT-CONFIG-001` | The body failed validation; `errors[]` lists each `{loc, msg}`. |
| 422 | <a id="problem-idempotency-key-reused"></a>`idempotency-key-reused` | `QT-USAGE-001` | The same `Idempotency-Key` was sent with a different body. |
| 429 | <a id="problem-rate-limited"></a>`rate-limited` | `QT-USAGE-001` | Rate limit reached; wait for `Retry-After` seconds. |
| 500 | <a id="problem-internal"></a>`internal` | `QT-ENGINE-001` | Unexpected error (no stack trace is returned; the `request_id` finds it in the logs). |
| 503 | <a id="problem-unavailable"></a>`unavailable` | `QT-ENGINE-001` | A dependency (database, object store) is down. |

Other HTTP errors raised by the framework carry the slug `http-error`. The `queuetruth.dev` problem URLs are
identifiers; they are not guaranteed to resolve to a web page in v0.1.
