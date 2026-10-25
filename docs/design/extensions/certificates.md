# Certificates and commitments (extension of SPEC 0.2, 3.10, 4.3, 4.4 and 7)

Status: built and tested (`python/queuetruth/cert/`, `python/tests/cert/`). This note is the contract the code was
built against, written to be merged into [the design specification](../spec.md) as a new section; the user guide
and the threat model are in [Certify a result](../../how-to/certify-a-result.md).

## C.1 Scope

A certificate records runs that already happened, so that a third party can re-hash every file they rest on and
re-execute them. It certifies the `--out` directory of `replay`, `zoo`, `calibrate`, `calibrate-live`, `fit-hazard`,
`score`, `referee run` and `dq` (run kinds `replay`, `zoo`, `calibrate`, `calibrate_live`, `fit_hazard`, `score`,
`referee`, `dq`). `ingest`, `shadows sample`, `features` and `report` write no summary with a spec echo into their
output, so their products are certified as inputs of other runs or as attachments. Service objects (`dataset_id`,
`upload_id`, `job_file` references) are refused (`QT-INPUT-002`): a service job is certified by its operator.

## C.2 Run detection

The first match of, in this order: `calibration.json` (`qt.calibration_result`; kind `calibrate_live` when its `kind`
is `live`), `model.json` (`qt.hazard_model_card`), `referee_report.json` (`qt.referee_report`), `dq_report.json`
(`qt.dq_report`), `summary.json` (`qt.replay_summary`, `qt.zoo_summary`, `qt.scorecard_summary`). The document must
carry a `spec` object. Its recorded run id is `run_id`, or the suffix after the last `:` of a hazard `model_id`;
`referee_report` and `dq_report` record none. `calibrate-live` adds `calibration_source: "live"` to its echo after
computing `run_id`; that annotation is dropped (`queuetruth.cert.runs.executable_spec`) before the run id is
recomputed and before the run is executed again, and kept in the certificate's `spec`.

## C.3 Certify

- Labels: `DIR` (label = directory name) or `LABEL=DIR`; labels match `^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}$` (never
  `.` or `..`: a label names the directory a re-execution is written to) and are unique. Output, input-file and
  evidence paths are relative POSIX paths with no empty, `.` or `..` component (schema pattern).
- Base: every relative spec path resolves against `--base` (default: the working directory), the directory the
  commands ran in.
- Inputs: every `{"path": ...}` reference of the spec echo (in document order, JSON pointer recorded) is hashed once
  per resolved path. A file: its sha256. A directory: every regular file under it (sorted by POSIX relative path;
  a symbolic link is `QT-INPUT-002`). Identity (the value that enters `run_id`, SPEC 7.3): the file sha256; for a
  directory whose `manifest.json` is a `qt.dataset_manifest`, its `dataset_id` (kind `dataset`); otherwise what
  `queuetruth.score._io.resolve_ref` returns (the sha256 of `manifest.json`, else a digest over the files). A
  dataset is `dataset_consistent` when every partition file listed in its manifest exists with the listed sha256
  and the SPEC 3.10 rule re-derives `dataset_id`; `certify` refuses an inconsistent dataset (`QT-INPUT-002`),
  because its identity, read from the manifest, would not change with its files. `data_ts_min` / `data_ts_max` are
  the extremes of the partitions' `ts_min` / `ts_max`.
- Binding: `run_id` is recomputed with `queuetruth.schemas.runid.run_id(spec_echo, engine_version, identity)`,
  `engine_version` from the document, else the installed one. When the document records a run id and it differs,
  `certify` fails with `QT-INPUT-002` (details: run, recorded, recomputed). `binding` is `verified` (recorded and
  equal) or `recomputed` (none recorded). Binding shows that the summary document names exactly these inputs, this
  spec and this engine version (nothing was mixed up); the recorded run id is in an output the issuer wrote, so
  only `--rerun` shows that the outputs follow from the inputs.
- Outputs: every file under the run directory, raw sha256 and `normalized_sha256`: for a `.json` file that parses
  and holds only finite numbers, the sha256 of `canonical_json` (RFC 8785) of the document without `timings` at any
  depth and without `out` / `threads` in its top-level `spec`; otherwise the raw sha256.
- Claims: `--claim TEXT`, ids `c1, c2, ...`. A reference is `{RUN#POINTER}` or `{RUN:FILE#POINTER}` (`POINTER` an
  RFC 6901 pointer starting with `/`; `FILE` defaults to the run's document); `{{` and `}}` are literal braces; any
  other brace is `QT-USAGE-001`. The file must be a JSON output of the run, the pointer must exist and must not
  contain a `timings` token. `rendered` replaces each reference with the value (a string verbatim, anything else as
  canonical JSON). A claim without references is kept with a warning.
- Attachments: `--attach PATH` hashed like inputs (ids `att1, ...`), not re-executed.
- Commitments: `--commitment C --opening O` pairs (C.6). The opening must open every listed item; revealed items are
  matched to input and attachment files by sha256; `earliest_data_ts` is the minimum `data_ts_min` of the datasets
  of the runs that use a matched input; `precedes_data = committed_at < earliest_data_ts` (null when undefined).
- `--out` and `--html` that equal or lie inside a certified run, input, attachment, commitment or opening path are
  `QT-USAGE-001`. A commitment whose `n_items` or `root` does not recompute from its leaves is `QT-INPUT-002`.
- `issued_at`: `SOURCE_DATE_EPOCH` (seconds) when set, else the wall clock, in ns.
- Environment: queuetruth, `_core` and engine versions, Python version and implementation, `system-release`,
  machine, versions of numpy, pyarrow, scipy, scikit-learn, pydantic, cryptography (when installed), and the values
  of `OMP_NUM_THREADS`, `MKL_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `PYTHONHASHSEED` when set. Never a hostname, a
  user name or an absolute path of the certifying process (spec echoes keep paths as given).

## C.4 Sealing and signing

Body = the document without `certificate_id` and `integrity`. `integrity.body_sha256 = sha256(canonical_json(body))`
and `certificate_id` = its first 16 hex characters. `canonical_json` is SPEC 7.3's: RFC 8785 except that integers
are written exactly (`issued_at`, `committed_at` and `data_ts_*` exceed 2^53, which a JCS library writing every
number as a double would round); `canonicalization: "rfc8785"` names this variant. `verify-cert` refuses a
certificate with a duplicate JSON object key, so every parser reads the same body. With `--key`, `integrity.signature = {algorithm: "ed25519",
public_key: hex(raw 32 bytes), key_fingerprint: sha256(raw public key) hex, value: hex(64-byte signature)}` over the
UTF-8 bytes of `"queuetruth.certificate.v1" + "\n" + canonical_json(body)` (pure Ed25519, RFC 8032). Commitments use
the domain `queuetruth.commitment.v1` over the commitment without `signature`. Keys: PKCS#8 PEM private keys (mode
0600; encrypted with `QT_CERT_KEY_PASSPHRASE` when set, `QT-CONFIG-001` when an encrypted key is loaded without it),
SubjectPublicKeyInfo PEM public keys at `<stem>.pub.pem`. Signing and signature checks import `cryptography` on use
(`QT-USAGE-001` with an install hint when it is missing).

## C.5 Verify

`verify-cert CERT [--rerun] [--only RUN]... [--base DIR] [--map OLD=NEW]... [--trust FPR]... [--pubkey PEM]...
[--require-signature] [--work DIR] [--force-rerun] [--timeout S] [--out FILE] [--html FILE]`. Checks in order
(`fail` fails the verdict; `warn`, `skip` do not):

| Id | Fails when |
|---|---|
| `schema` | the document is not a valid `qt.certificate`, or run labels, input ids or claim ids repeat (no other check runs) |
| `integrity` | the body digest or `certificate_id` does not recompute |
| `signature` | the signature does not verify, or the fingerprint is not the sha256 of the key; unsigned = `warn` (`fail` with `--require-signature`) |
| `key_pin` | with `--trust` / `--pubkey`, the signature does not verify or the signer is not one of them; signed without a pin = `warn` |
| `input:<id>`, `attachment:<id>` | a file is missing, changed or of another size, the identity or kind changed, or for a dataset the manifest, `dataset_id`, `dataset_consistent` or `data_ts_min`/`data_ts_max` do not re-derive; extra files = `warn` |
| `outputs:<run>` | an output is missing, changed or of another size, or its `normalized_sha256` does not recompute from the file; extra files = `warn` |
| `binding:<run>` | the document's kind, schema, spec or recorded run id changed, the certified inputs are not exactly the spec's references at the paths it names, or `run_id` does not recompute from the current input identities |
| `claim:<id>` | the evidence is not exactly the references of `text` (run, file defaulting to the run's document, pointer, spelling), a cited file is not a certified output or failed its hash check, a cited value differs (canonical JSON), or `rendered` does not follow; `skip` when it cites a run outside `--only` |
| `commitment:<n>` | the commitment file changed or is inconsistent, a revealed item does not open its leaf, its matched inputs do not recompute from the certified file hashes, its signature fails, a linked input failed, or `earliest_data_ts` / `precedes_data` do not recompute |
| `rerun:<run>` | (`--rerun`) the child process failed, or a certified output is missing or has another normalized sha256 than the certified file on disk (outside the SPEC 7.2 tolerance); `warn` for outputs equal only within it and for extra files; `skip` after a failed `integrity`, `signature` or `key_pin` unless `--force-rerun` |
| `rerun_claim:<id>` | (`--rerun`) a cited value read from the re-executed outputs differs (`warn` within 4 ulp) |

Paths resolve with `--map` (first matching `OLD` prefix, whole components) and then against `--base`. `--only`
limits runs, their inputs and claims. Re-execution: `python -P -m queuetruth.cert.rerun` (`PYTHONSAFEPATH=1`: the
working directory is not on the import path) with the working directory set to the base, `{"kind", "spec"
(annotations dropped, mapped, `out`/`threads` removed at every depth, `threads` from `--threads` for replay, zoo
and calibration), "out"}` on stdin, running the `queuetruth.api` function of the kind (`replay`, `run_zoo`,
`calibrate`, `calibrate_live`, `fit_hazard`, `score`, `referee`, `dq`) with `out` set to `<work>/<label>`, a
directory that must not exist yet. `<work>` is a temporary directory removed afterwards, or `--work`, which must
be new or empty and must not be, contain or lie inside a certified path (`QT-USAGE-001`); nothing existing is ever
deleted. Timeout `--timeout` (default 6 h). Re-executed outputs are compared with the normalized sha256 of the
certified files on disk; under `--map`, the mapped prefixes are first written back (`NEW` -> `OLD`) in every JSON
string, in other files at path-component boundaries, and in the `sha256`/`bytes` a JSON object records of a file
so rewritten. A JSON or Parquet output that still differs is compared by value (`queuetruth.cert.tolerance`: same
structure, schema, metadata, nulls and non-float values; float32/float64 within 4 ulp, NaN = NaN): `equivalent`
(`warn`) when it agrees. Differences of the queuetruth version, platform and machine, or any recorded package
version, between the certificate's `environment` and the verifier are warnings. The `qt.cert_verification`
document lists the checks (details capped at 20 lines each), per-run `inputs_ok`, `outputs_ok`, `binding_ok`,
`rerun` (`identical`, `equivalent`, `differs`, `failed`, `skipped`, `not_run`), `differing` and `within_tolerance`
files; `key_trusted` is true only when the signature verifies and its key is pinned. Exit codes: 0 pass, 1 fail
(`QT-GATE-001` on stderr), 2 error (unreadable certificate, duplicate JSON keys, bad usage).

## C.6 Commitments

`commit FILE... --out C [--opening O] [--name NAME]... [--label TEXT] [--key PEM]`: per file a fresh 32-byte nonce
(`secrets.token_hex`), `leaf_i = sha256(b"qt-commit-v1\0leaf\0" || nonce_i || utf8(name_i) || b"\0" ||
sha256(file_i))` (names unique, no NUL), `root = sha256(b"qt-commit-v1\0root\0" || leaf_0 || ... || leaf_(n-1))`.
`qt.commitment` = {scheme `qt-commit-v1`, label?, committed_at (ns, `SOURCE_DATE_EPOCH` rule), n_items, leaves,
root, signature?}; `qt.commitment_opening` = {root, items [{index, name, bytes, sha256, nonce}]}, written with mode
0600 (default path `<out stem>.opening.json`); an existing commitment or opening is not overwritten without
`--force`. Any subset of the items is a valid opening: `cert partial-opening O --item NAME... --out P [--force]`
writes one.

`reveal C FILE... --opening O [--trust FPR]... [--pubkey PEM]...`: checks `structure` (root and `n_items` recompute),
`signature`, `key_pin`, `opening` (each opened item's leaf; an opening of another root is `QT-INPUT-002`) and
`content` (each opened item matches a given file by sha256, each given file matches an opened item; `skip` when no
file is given). Items are `match`, `leaf_mismatch`, `content_mismatch` or `not_given`. Exit 1 when a check fails.

## C.7 Documents (JSON Schemas generated from `queuetruth.cert.models`, registered in `DOC_MODELS`)

- `qt.certificate`: certificate_id, title?, issuer?, issued_at, environment {queuetruth, core?, engine_version,
  python, implementation, platform, machine, packages {name: version}, env {name: value}}, runs [{label, kind,
  path, document, document_schema, run_id, binding, engine_version, spec, inputs [{pointer, input}], outputs [{path,
  bytes, sha256, normalized_sha256}], reproduce {api, python, verify}}], inputs [{id, path, kind
  (`dataset`/`directory`/`file`), identity, bytes, files [{path, bytes, sha256}], dataset_id?, dataset_consistent?,
  data_ts_min?, data_ts_max?}], claims [{id, text, rendered, evidence [{ref, run, file, pointer, value}]}],
  attachments (same shape as inputs), commitments [{path, sha256, root, label?, committed_at, key_fingerprint?,
  revealed [{index, name, sha256, nonce, inputs}], earliest_data_ts?, precedes_data?}], reproduce [str], warnings
  [str], integrity {canonicalization `rfc8785`, domain `queuetruth.certificate.v1`, body_sha256, signature?}.
- `qt.cert_verification`: certificate {path, certificate_id?, body_sha256?, issued_at?, signed, key_fingerprint?,
  key_trusted?}, rerun, status (`pass`/`fail`), counts {pass, warn, fail, skip}, checks [{id, status, summary,
  details}], runs [{label, kind, run_id?, inputs_ok, outputs_ok, binding_ok, rerun, differing}], warnings.
- `qt.commitment`, `qt.commitment_opening`: C.6. `qt.commitment_verification`: commitment {path, root?, label?,
  committed_at?, n_items?, signed, key_fingerprint?, key_trusted?}, status, n_revealed, revealed [{index, name,
  sha256, status, file?}], unmatched_files, checks.
- `qt.signing_key`: algorithm `ed25519`, public_key, key_fingerprint, private_key_path?, public_key_path?,
  encrypted? (never private key material).

`cert render CERT --out HTML [--verification V]` prints a `qt.report_summary`; a verification of another
certificate (other `body_sha256` or `certificate_id`) is `QT-INPUT-002`. Without a verification the page says the
signature was not verified; with one, it shows the signature and key-pin results.

## C.8 Command line

The five commands (`certify`, `verify-cert`, `commit`, `reveal`, `cert keygen|pubkey|render`) are parsed in Python
(`queuetruth.cert.cli`): `queuetruth.cli.main` hands any argument list whose first word (after global flags) is one
of them to `queuetruth.cert.cli.maybe_main` before the clap grammar, and `queuetruth help COMMAND` for them to
`COMMAND --help`. Global flags `--format/--output-format`, `--quiet`, `--verbose`, `--log-level`, `--threads N`
(passed to re-executed replay, zoo and calibration runs) and `--seed N` (accepted, ignored: a certified run's seed
is in its spec) are accepted before or after the command. They are not in `crates/cli/src/grammar.rs`, so
`queuetruth --help` does not list them and the standalone `queuetruth-core` binary does not have them.

## C.9 Not built

- No HTTP or MCP endpoint (the vision's `certify_run` tool): the service would need tenant-scoped storage of
  certificates and its own signing key.
- No timestamp-authority client: the how-to shows how to request an RFC 3161 token for a root with `openssl ts`;
  parsing and verifying the reply is left to `openssl ts -verify`.
- No key revocation or key rotation records.
- `calibrate-live` computes its `run_id` before adding `calibration_source` to the echo, so the echo is not what
  SPEC 7.3 hashed and every verifier needs the `executable_spec` exception (C.2). The upstream fix is to compute
  the run id from the echo as written, or to keep annotations outside `spec`.
