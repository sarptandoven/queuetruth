# Certify a result

**Goal:** hand someone a fill-realism result (a replay, a zoo leaderboard, a calibration, a scorecard, a referee
report) together with a file that lets them check, without trusting you, that the numbers you quote are the numbers
the runs produced, that the runs used the inputs you name, and that running them again gives the same bytes.

`queuetruth certify` writes that file, a `qt.certificate`. `queuetruth verify-cert` checks it, and with `--rerun`
executes every run again and compares every output file. `queuetruth commit` and `queuetruth reveal` add the
missing piece for forecasts and order schedules: proof that a file was fixed before you saw the data it is scored
against, as far as an independent timestamp of the commitment allows.

**You need:** `pip install queuetruth` and, to sign or check signatures, `pip install "cryptography>=42"`
(unsigned certificates and commitments need nothing else). The examples run on the synthetic referee fixtures, so they work offline.

## 1. Make the runs you want to certify

A certificate covers the `--out` directories of `replay`, `zoo`, `calibrate`, `calibrate-live`, `fit-hazard`,
`score`, `referee run` and `dq`: each holds a summary document with the spec echo the run can be executed again
from. Run the commands from one directory and keep relative paths: `certify` and `verify-cert` resolve them against
that directory (`--base` names another one).

<!-- check-docs: run -->
```sh
queuetruth referee fixtures --out fx > /dev/null
D=fx/02_level_shrink_caps_queue
queuetruth shadows sample --data $D/dataset --n 20 --seed 1 --window-open-s 0 --window-close-s 0 \
  --out shadows.parquet > /dev/null
queuetruth replay --data $D/dataset --shadows shadows.parquet --out replay > /dev/null
queuetruth referee run --data $D/dataset --schedule $D/schedule.parquet --sim-fills $D/expected_fills.parquet \
  --out referee > /dev/null
ls replay referee
```

## 2. Certify them with claims

Each `--claim` is free text in which `{RUN#/json/pointer}` (or `{RUN:FILE#/json/pointer}` for another JSON output
of the run) cites a result value; `{{` and `}}` are literal braces. A run is named after its directory, or
`LABEL=DIR` (1 to 64 letters, digits, `_`, `-` or `.`, not starting with a dot). `certify` stores the exact value
next to the claim and fills it into the text.

<!-- check-docs: run -->
```sh
queuetruth certify replay referee --out cert.json --html cert.html \
  --title "Referee fixture 02" \
  --claim "replay filled {replay#/shadows/n_filled} of {replay#/shadows/n} shadow orders" \
  --claim "referee pass: {referee#/pass}, discordant sim-only fills: {referee#/discordant/sim_only}"
```

Before it writes anything, `certify`:

- hashes every input the specs name (each file of a dataset or live-log directory), and checks that a dataset's
  `manifest.json` partition hashes match its files and re-derive its `dataset_id`;
- recomputes each `run_id` (SPEC 7.3) from the spec echo, the engine version and those input identities, and
  refuses when it differs from the run id the summary records: the inputs on disk are then not the inputs the run
  used (changed since, another directory, or another queuetruth version);
- hashes every output file, raw and normalized (JSON canonicalised without `timings` and without the
  execution-only `out`/`threads` of the spec echo, the determinism rule of SPEC 7.2);
- resolves every claim reference, refusing values under `timings` (wall-clock, never reproducible) and files that
  are not JSON outputs of a certified run.

The certificate also records the environment (queuetruth, core and engine versions, Python, platform, numeric
package versions, thread-count variables; never a hostname, user name or absolute path of its own), the reproduction
commands, and per run a one-line Python call that re-executes it. `referee` and `dq` reports record no run id, so
for them only `--rerun` binds outputs to inputs; `certify` says so in a warning.

## 3. Verify, then re-execute

<!-- check-docs: run -->
```sh
queuetruth verify-cert cert.json
queuetruth verify-cert cert.json --rerun --verbose
```

Without `--rerun` the checks are:

- `integrity`: the sha256 of the canonical body (RFC 8785 with integers written exactly, SPEC 7.3);
- `signature` and `key_pin`;
- `input:<id>`: every input file re-hashed and its size compared, a dataset's manifest, `dataset_id` and data time
  span re-derived;
- `outputs:<run>`: every output file re-hashed, raw and normalized;
- `binding:<run>`: the summary's kind, schema and spec echo unchanged, each input at the path its spec names, and
  the run id recomputed from the current inputs;
- `claim:<id>`: the evidence is exactly the references in the claim text, each cited file is a certified output
  that passed its hash check, and each cited value read again equals the stored one;
- `commitment:<n>`.

A missing or changed file fails; a file that was added warns. With `--rerun`, each run is executed again in a child
process from the certified spec, every certified output must come back with the same normalized sha256, and
`rerun_claim:<id>` re-reads each cited value from the new outputs. The outputs go to a temporary directory, or with
`--work DIR` to a new or empty directory that `verify-cert` fills with one subdirectory per run and leaves in place;
it refuses a `--work` that overlaps a certified path and never deletes or replaces an existing file. Re-execution
is skipped when `integrity`, `signature` or `key_pin` failed, since the certificate is already known to be bad
(`--force-rerun` runs it anyway). `--threads N` sets the worker threads of re-executed replay, zoo and calibration
runs (outputs do not depend on it) and `--timeout S` limits each run (default 6 h). `--only RUN` limits the work to
some runs. The exit code is 0 when no check failed and 1 when one did (`QT-GATE-001`); `--format json` prints the
`qt.cert_verification` document, `--out` saves it and `--html` renders the page with the results.

A certificate moved to another machine verifies against a copy of the inputs and runs: `--base DIR` for relative
paths, `--map OLD=NEW` for absolute ones. Spec echoes keep paths as given (SPEC 7.2), so a run re-executed under
`--map` writes the new location into its summaries and reports. Before comparing, `verify-cert` writes those paths
back as the certified ones: every JSON string that is such a path, every occurrence in other files (an HTML report),
and the sha256 and size a JSON summary records of a file it rewrote.

## 4. Sign it

<!-- check-docs: run -->
```sh
queuetruth cert keygen --out keys/me.pem
queuetruth certify replay referee --out signed.json --key keys/me.pem \
  --claim "replay filled {replay#/shadows/n_filled} of {replay#/shadows/n} shadow orders"
queuetruth verify-cert signed.json --pubkey keys/me.pub.pem
```

`cert keygen` writes an Ed25519 private key (PKCS#8 PEM, mode 0600, encrypted with the passphrase in
`QT_CERT_KEY_PASSPHRASE` when that variable is set) and `me.pub.pem` next to it, and prints the key fingerprint, the
sha256 of the raw 32-byte public key. `cert pubkey FILE` prints it again. The signature covers
`queuetruth.certificate.v1`, a newline and the canonical body, so a certificate signature can never pass as a
commitment signature. A verifier pins the signer with `--trust FINGERPRINT` or `--pubkey PEM`; without a pin a
valid signature only proves that the body was not changed after someone holding that key signed it, and
`verify-cert` warns. `key_pin` passes only when the signature also verifies. `--require-signature` fails unsigned
certificates.

## 5. Commit before the data, reveal after

`commit` fixes files (an order schedule, a shadow spec, a forecast) under one root digest without revealing them:

<!-- check-docs: run -->
```sh
queuetruth commit shadows.parquet --label "schedule for 2026-06-15" --out commitment.json
ls -l commitment.opening.json
queuetruth reveal commitment.json shadows.parquet --opening commitment.opening.json
```

The public `qt.commitment` holds one leaf per file, sha256 over a fresh 32-byte nonce, the file's name and the
file's sha256, and a root over the leaves. Without the nonces it reveals nothing about the files but their number,
even a one-line forecast that could otherwise be guessed and hashed. The private `qt.commitment_opening` (mode
0600) holds the nonces; any subset of its items opens exactly those files, and `queuetruth cert partial-opening
O.json --item NAME --out PART.json` writes the opening of the named items only, to reveal one file and keep the
others hidden. Publish or hand over the commitment now, keep the opening until you reveal. `commit` refuses to
overwrite an existing commitment or opening (the opening holds the only copy of the nonces; `--force` replaces
them). `--key` signs a commitment like a certificate.

A certificate can carry the link. `certify --commitment commitment.json --opening commitment.opening.json` checks
the commitment's structure and the opening, matches each revealed file to the certificate's inputs by sha256, and
records whether the commitment's `committed_at` precedes the earliest `ts_event` of the datasets used by the runs
that consume those inputs (`precedes_data`). `verify-cert` recomputes the matches from the certified file hashes
and the earliest `ts_event` from each dataset's `manifest.json`, so neither can be edited into a certificate.

<!-- check-docs: run -->
```sh
queuetruth certify replay --out linked.json --commitment commitment.json --opening commitment.opening.json
queuetruth verify-cert linked.json --only replay
```

`committed_at` is the committer's clock. To make the time evidence, get the root independently timestamped
before the data exists, for example with an RFC 3161 time-stamping authority (`openssl ts -query -digest ROOT
-sha256 -cert -out root.tsq`, then send `root.tsq` to the authority of your choice and keep its reply), a public
post or a counterparty's receipt. queuetruth contacts no timestamp service itself.

## 6. What tampering looks like

<!-- check-docs: run exit=1 -->
```sh
printf 'x' >> replay/trades.parquet
queuetruth verify-cert cert.json
```

The failed check names the file (`outputs:replay`, `changed: trades.parquet`). An edited certificate fails
`integrity`; an edited and re-hashed unsigned certificate passes `integrity` but fails the claim, input, output,
binding or commitment checks it no longer matches, and a certificate whose outputs were edited before certifying
passes every file check and fails `--rerun`.

## What a certificate proves

| It shows | Because |
|---|---|
| The listed files are byte for byte the certified ones | sha256 of every input and output file |
| The summary names exactly these inputs, this spec and this engine version | the `run_id` recomputed from the summary's spec echo, the engine version and the current input identities equals the one the summary records (referee and dq summaries record none) |
| The outputs follow from those inputs and that spec under this queuetruth version | `--rerun` reproduces every output's normalized sha256 |
| Each quoted number is the number in the output | the cited value is stored, re-read and, with `--rerun`, recomputed |
| The holder of a key sealed this exact body | Ed25519 over the canonical body, if you pinned the fingerprint |
| Committed files could not be swapped after the commitment was seen | sha256 binding; the 256-bit nonce keeps them hidden until revealed |

## What it does not prove (threat model)

- **That the outputs came from the inputs, without `--rerun`.** The run id the binding check recomputes is recorded
  in the summary, an output file the issuer wrote. A fabricated summary with the right spec echo and run id passes
  every check but re-execution, which is why section 6 ends with `--rerun`.
- **That the inputs are real.** A certificate binds results to inputs, not inputs to the market or to a broker.
  Synthetic or fabricated order logs, fills and datasets certify as well as real ones; a dataset's
  `validation_status` is declared by whoever ingested it. Check provenance separately: re-download public data and
  compare hashes (`dataset_id`, the manifest's input hashes), and reconcile live fills with the venue's statements.
- **That nothing else was tried.** The certificate covers the runs it lists. Specs, models or parameter sets tried
  and dropped before it (selection, the garden of forking paths) are invisible. Committing the plan and the order
  schedule before the data, and keeping a ledger of every trial, is the only defence.
- **When anything happened.** `issued_at` and `committed_at` are the issuer's and the committer's clocks. Only an
  independent timestamp of the digest (RFC 3161, a public post, a counterparty) dates it.
- **That the engine is right.** Re-execution reproduces bugs as faithfully as correct results. Correctness rests on
  the oracles and real-data validation in [the results](../RESULTS.md), not on the certificate.
- **Reproduction across versions and platforms.** Determinism is promised byte for byte for the same inputs, spec
  and version on one platform (SPEC 7.2). Across platforms, integer outputs are identical and values computed with
  `powf`, `ln` or `exp` may differ by up to 4 ulp. A JSON or Parquet output that differs only that much is reported
  as `equivalent` (a warning, never `identical`); any other difference fails. Replay and referee outputs hold
  integers and ratios, which IEEE arithmetic rounds the same everywhere, so they are expected to come back
  identical; zoo, calibration and fit-hazard outputs use `exp` and `ln` and can come back equivalent. Other packages matter too: Parquet files written from Python carry pyarrow's
  version in their metadata (the tolerant comparison reads the data, not the metadata), and fit-hazard's model
  pickle depends on the scikit-learn and numpy versions and must match byte for byte. `verify-cert --rerun` warns
  when the queuetruth version, the platform or any recorded package version differs from the certificate's. No
  cross-platform re-execution has been measured yet.
- **Who signed, without a pin.** A valid signature by an unknown key proves only integrity. There is no key
  revocation: if a private key leaks, certificates signed with it prove nothing from then on; keep it encrypted.
- **Anything, unsigned.** The digest of an unsigned certificate detects accidental change only; whoever edits it
  can recompute the digest. The file checks still catch edits that no longer match the files.
- **Secrecy of what is certified.** A certificate is not confidential: it carries spec echoes (paths as given), file
  names, sizes, sha256 digests and the cited values. A hash of a small, guessable file (a short order list) can be
  confirmed by guessing; use a commitment, which is salted, to keep content hidden until reveal. Certificates hold
  no market data, so a verifier needs their own licensed copy to re-run (see
  [data and licensing](../data-and-licensing.md)).
- **Safety of verifying a stranger's certificate.** Every name a certificate uses as a path is checked before it
  is used: run labels are plain names, output and evidence files are relative paths inside their run directory, and
  a claim can cite only a certified output. `--rerun` runs queuetruth's own commands on the certified specs in a
  child process whose import path never includes the working directory (`python -P`), so a `json.py` or
  `queuetruth/` placed next to the data is not imported; the data files are parsed by queuetruth's own readers.
  Outputs go to a directory `verify-cert` creates, and nothing that existed is deleted or replaced. But the specs
  can name any readable path on your machine, and a run can take long or use much memory. Read the specs (`--html`)
  and use `--base`, `--map` and `--timeout` before re-running a certificate you did not make.

## Measured

On 2026-10-25, on the reference Mac in Low Power Mode with a load average between 4 and 5:

- a replay of the 2019-12-30 Nasdaq ITCH dataset (4 instruments, 6,103,262 records, 3,000 shadow orders, rank
  oracle; 9 output files, 21,208,187 bytes) certified with a claim citing 4 values; `verify-cert` took 1.2 s and
  `verify-cert --rerun` 4.0 s, with all 9 outputs identical (on 2026-10-24, at a load average of 250 to 290, the
  same took 2.4 s and 8.8 s);
- `verify-cert --rerun` reproduced all 38 output files of the synthetic pipeline of the end-to-end tests, one run
  of each of the eight kinds, byte for byte: replay (9 files), zoo (6), calibrate (8, including its held-out zoo),
  calibrate-live (2), score (7), fit-hazard (3, including the model pickle), referee (1) and dq (2). That run is the
  `integration` test `python/tests/cert/test_kinds.py` (`pytest -m integration python/tests/cert`), 43 s in all.

The behaviours on this page are covered by `python/tests/cert/` (round trips, tampering, forged certificates,
signatures, re-execution, the cross-platform tolerance, commitments, the command line and the HTML page).
