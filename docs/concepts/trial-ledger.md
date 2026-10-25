# Trial ledger

A backtest that looks good after many tries is weaker evidence than the same backtest found on the first try.
Selection statistics correct for that (the Deflated Sharpe Ratio, the probability of backtest overfitting, White's
Reality Check and Hansen's SPA, model confidence sets), and every one of them needs an input that users almost never
keep: **how many trials were run** to find the result being claimed. The trial ledger keeps that count. It is an
append-only, hash-chained local file that records every `zoo`, `calibrate`, `calibrate-live`, `fit-hazard`, `score`
and `shadows sample` run, from the Python API or the CLI, plus the strategy variants you register yourself.

The count is a **lower bound** of what was tried, never more. A trial leaves no record when it runs with
`QT_LEDGER=off`, against another ledger (another `QT_LEDGER_DIR`, another project, the per-user ledger), after the
ledger file was deleted (the next run starts a new ledger with a new `ledger_id`), or outside queuetruth without
`register_variant`. A hash chain shows edits; it cannot show omissions. So cite the `ledger_id`, the genesis time
and the `head` with any claim: a referee can then ask for the whole chain since genesis and check it
(`ledger verify --anchor`).

The ledger is a side output. It never changes a spec, a `run_id`, an output file or a summary, and an entry point
never fails because recording failed (the failure is logged as a warning). A test runs the synthetic pipeline with
the ledger on and with `QT_LEDGER=off` and checks that every output file and every summary is byte-identical
(`python/tests/ledger/test_ledger_e2e.py`).

## What is recorded

| Kind | Recorded by | `run_id` and `trial_id` | Headline `metrics` |
|---|---|---|---|
| `shadows-sample` | `queuetruth shadows sample`, `queuetruth.sample_shadows` | recomputed from the summary's spec echo (SPEC 7.3) | `n` |
| `zoo` | `queuetruth zoo`, `queuetruth.run_zoo` | the summary's `run_id` | `n_shadows`, `n_models`, `n_leaderboard`, `n_excluded_hbt_312`, `brier_mean:<model>` (mean Brier over the horizons), `best_model`, `best_margin` |
| `calibrate` | `queuetruth calibrate ...`, `queuetruth.calibrate` | the summary's `run_id` | `kind`, `family`, `param_name`, `estimate`, `loglik`, `n_events`, `n_shadows`, `trust_verdict`, `held_out_brier_mean`, `baseline_held_out_brier_mean` |
| `calibrate-live` | `queuetruth calibrate-live`, `queuetruth.calibrate_live` | the summary's `run_id` | as `calibrate` |
| `fit-hazard` | `queuetruth fit-hazard`, `queuetruth.fit_hazard` | the `run_id` inside `model_id` | `model_id`, `n_rows_train`, `recalibration`, `test_*` scores |
| `score` | `queuetruth score`, `queuetruth.score` | the summary's `run_id` | `n_orders`, `n_matched`, `twin_match_rate`, `signed_bias`, `gates_passed`, `headline` |
| `variant` | `queuetruth ledger add`, `queuetruth.ledger.register_variant` | hash of (name, params, dataset, code version, outcome) | your own |

The zoo's own selection (the best of `n_models` fill models, by how much) and a calibration's held-out score against
the default are in `metrics`, so "which model won, out of how many, by how much" can be answered from the ledger.

Every record also carries:

- `spec_hash`: the first 16 hex digits of the sha256 of the configuration, which is the spec with the
  execution-only fields (`out`, `threads`), the schema 1.1 fields at their default and every data reference removed.
  The same configuration run on two datasets has one `spec_hash` and two `run_id`s.
- `inputs` and `dataset_hash`: the identity of every data reference of the spec, by field path, exactly as that
  run's `run_id` resolves it (the Rust core for `zoo`, `calibrate` and `shadows sample`, the Python layer for the
  others: a dataset by its `dataset_id`, a file by its sha256, a live log by its manifest), and the sha256 of their
  canonical JSON. For a variant, `inputs` holds `dataset`, the content identity of the path you gave (a dataset
  directory's `dataset_id`, a file's sha256), or `dataset_label`, a free label that is not bound to any data.
- `code`: the queuetruth package, engine and `_core` versions, the commit of the source checkout queuetruth was
  imported from (null for an installed wheel), the commit of the git checkout holding your working directory
  (`project_git`), and for a variant your own `code_version`. Uncommitted changes are not reflected.
- `study`: the group of trials searched for one claim (`QT_LEDGER_STUDY`, `with queuetruth.ledger.study(...)`, or
  `--study`; default `default`). Selection statistics count the trials of one study.
- `started_at_ns` and `recorded_at_ns`: wall-clock times (UTC ns). They live only in the ledger, never in an output.
- `status` and `error`: a run that raised is recorded with its `QT-*` code and message, its `spec_hash` taken from
  the spec as given, and no `trial_id`. So is a result the CLI refuses with exit 2 (`score` that matched no live
  order, a calibration that fitted no event), with the same `QT-INPUT-002` the user saw.
- `series`: an optional per-trial series (below), stored out of line in `series/<sha256>.qts` next to the ledger;
  the record keeps its sha256, length and kind, so the chain covers the values while counting never reads them.

A nested entry point is part of its caller's trial: the zoo run inside `score` or `calibrate-live` is not a second
record.

## How trials are counted

`n_trials` counts distinct trials:

- **A rerun is not a new trial.** Outputs are deterministic (SPEC 7.2), so the same spec on the same data at the
  same engine version (one `run_id`) is the same trial however often it runs. Registering the same variant
  evaluation twice is also one trial.
- **A different outcome is a new trial.** A variant's identity includes its outcome: its series, or without one its
  metrics. Editing the strategy code and registering the same name, parameters and data again with a different
  result is a new trial even when no `code_version` was given; a different parameter, dataset or `code_version` is
  one too. A run whose records carry k different series (you attached disagreeing series with `record`) counts k
  times, and each series is listed.
- **A run a variant was evaluated with is part of that variant's trial.** When a selected variant names a run in
  `run_ids`, the run is not counted again (`n_linked` in `ledger stats`).
- **A failed run is not a trial.** It produced no outcome. It is still recorded, and `ledger stats` reports it under
  `n_errors`, so a search that crashed half its configurations is visible.

`ledger stats` also reports `n_configs` (distinct `spec_hash`es across datasets) and `n_conflicts` (trials whose
records carry different series). Without a filter `n_trials` adds up every kind and study; the count for one search
is `n_trials(study=..., kinds=["variant"])`, which is what `selection_inputs` uses by default.

## Where the ledger lives

`ledger.jsonl` (and its `series/` directory) in `$QT_LEDGER_DIR` when it is set; otherwise in
`<project root>/.queuetruth/ledger/`, where the project root is the nearest directory from the working directory
upwards that holds `.queuetruth/`, `pyproject.toml` or `.git`; outside any project, in the per-user ledger
(`~/Library/Application Support/queuetruth/ledger` on macOS, `$XDG_DATA_HOME/queuetruth/ledger` or
`~/.local/share/queuetruth/ledger` elsewhere, `%LOCALAPPDATA%\queuetruth\ledger` on Windows). The default therefore
depends on the working directory: a notebook and a terminal in different directories can write to different ledgers.
Set `QT_LEDGER_DIR` for one stable ledger per research project.

A run is never recorded inside a directory it reads: a live log or dataset directory without a manifest is
identified by every file under it, so a ledger there would change the next run's `run_id`. When the ledger would lie
inside an input directory, the run is recorded in the per-user ledger instead, with a warning.

When queuetruth creates the default project directory it writes a `.gitignore` containing `*` into it, so the ledger
stays out of git. Delete that file to commit the ledger, which puts its history under git's own hash chain as well;
but never append to a committed ledger on two diverging branches, because two chains that both extend the same head
cannot be merged without breaking the chain.

| Variable | Effect |
|---|---|
| `QT_LEDGER_DIR` | ledger directory (or a `.jsonl` file) |
| `QT_LEDGER=off` | record nothing (also `0`, `false`, `no`, `disabled`); explicit `record` and `register_variant` calls log a warning and return None; `on` forces recording |
| `QT_LEDGER_STUDY` | study of runs recorded without an explicit one |
| `QT_LEDGER_LOCK_TIMEOUT_S` | how long an append waits for the lock (default 30) |

The HTTP service never records automatically (a process that imported `queuetruth_service`), because one local file
would mix tenants (SPEC 8.8). Under pytest (`PYTEST_CURRENT_TEST` set) runs are recorded only when `QT_LEDGER_DIR` is
set or `QT_LEDGER=on`, so that a test suite never adds synthetic runs to a research ledger.

## Registering the variants you try

Register every variant you evaluate, including the ones you discard: the discarded ones are what the deflation
needs. A variant carries a name, parameters, optionally a series of per-period `returns` (fractions) or per-unit
`scores` with the period end of each value (`index`, UTC ns or datetimes), the data it was evaluated on (a path,
identified by content, or a label), your code version and the `run_id`s of the queuetruth runs it used.
`register_variants([...])` appends a whole grid in one locked write.

<!-- check-docs: run -->
```python
import os
import tempfile

os.environ["QT_LEDGER_DIR"] = tempfile.mkdtemp()
from queuetruth import ledger

days = ["2024-01-02", "2024-01-03", "2024-01-04"]
with ledger.study("momentum-es"):
    for lookback, rets in ((10, [0.004, -0.002, 0.003]), (20, [0.001, 0.002, -0.001]), (40, [-0.003, 0.0, 0.002])):
        ledger.register_variant(
            "momentum", {"lookback": lookback}, returns=rets, index=days, periods_per_year=252, dataset="ES 2024H1"
        )
    ledger.register_variant("momentum", {"lookback": 20}, returns=[0.001, 0.002, -0.001], index=days,
                            periods_per_year=252, dataset="ES 2024H1")  # the same evaluation again: one trial
    ledger.register_variant("momentum", {"lookback": 20}, returns=[0.002, 0.002, -0.001], index=days,
                            periods_per_year=252, dataset="ES 2024H1")  # code changed, result moved: a new trial

assert ledger.n_trials(study="momentum-es") == 4
sel = ledger.selection_inputs(study="momentum-es")
print(sel["n_trials"], sel["aligned"], [t["n_obs"] for t in sel["trials"]], sel["head"]["seq"])
```

The index above is given as ISO dates; integers are UTC ns, and `datetime`, `date`, `numpy.datetime64` and
`pandas.Timestamp` values work too.

From the shell, `queuetruth ledger add --name momentum --params '{"lookback": 20}' --returns daily.csv
--index-column date --periods-per-year 252 --dataset @data/es_2024h1` does the same (`--scores FILE` with
`--lower-is-better` for a loss). A series file is a `.json` array (or `{"values": [...], "index": [...]}`), a
`.parquet` table, or text with one row per line and columns separated by `,`, tab or `;`. `--column NAME|N` picks the
value column (default: the last) and `--index-column NAME|N` the period ends (ISO 8601 dates or times, UTC when no
offset, or ns). A first line is a header only when none of its cells starts like a number; any other cell that is
not a number is `QT-INPUT-002` naming its line, so `0.01%` fails loudly instead of being dropped.

## Selection inputs

`queuetruth.ledger.selection_inputs(filter, series="returns")` returns plain JSON data (`qt.selection_inputs`) for a
selection statistic to consume. It computes nothing from the series, and it reads the trials of **one search**: the
filter's kind defaults to `variant`, and a selection that spans more than one study or kind is refused
(`QT-CONFIG-001`) unless you pass `allow_mixed=True` (`--allow-mixed`).

- `n_trials`: every trial the filter selects, with or without a series, counted as above. Trials without a series
  still count; leaving them out would understate the search.
- `n_configs` and `by_kind`: distinct configurations (`spec_hash`) and trials per kind.
- `trials`: per trial, in order of first record, its `series` of the requested kind (`returns` or `scores`; null
  when it has none) with `index`, `n_obs`, `periods_per_year`, `higher_is_better` and `dataset_hash`. Each series is
  read from its file and checked against its sha256.
- `aligned` and `index`: whether every trial has a series with an index and all indexes are equal, and that index.
- `conflicts` and `linked_run_ids`: trials listed once per distinct series, and runs counted with their variant.
- `ledger_id`, `head` and `chain_verified`: which ledger and which point of its chain the numbers were read from.

What each statistic needs from it:

| Statistic | Needs |
|---|---|
| Deflated Sharpe Ratio | `n_trials` (N) and the variance of the trials' Sharpe ratios, from the trials that have a series; one study, one kind |
| PBO (CSCV), White's Reality Check, Hansen's SPA, model confidence sets | every trial's series on the same periods: `aligned` must be true; trials without a series cannot enter |

Deflate by `n_trials` when every trial is a distinct evaluation you looked at. When one configuration was evaluated
on several datasets (`n_trials` > `n_configs`), the per-dataset results are not independent searches over strategies;
`n_configs` is then the count of configurations tried, and `dataset_hash` per trial shows which data each result
came from. Either way the number is a lower bound (see the top of this page).

## Commands

| Command | Does |
|---|---|
| `queuetruth ledger list [filters] [--limit N]` | the newest records (default 20, `0` = all), oldest first |
| `queuetruth ledger stats [filters]` | trials, configurations, records and failed runs, by kind and by study |
| `queuetruth ledger export --out PATH [filters] [--selection [--series scores] [--allow-mixed]]` | `.jsonl` (the whole chain, byte for byte, with its series files copied to `series/` next to it, verifiable on its own; no filter), `.json` (`qt.ledger_dump`: records with their hashes and series values), `.parquet` (one row per record, series values included), or the selection inputs as `.json` |
| `queuetruth ledger verify [--anchor SEQ:HASH]` | the full integrity check; exit 2 with `QT-LEDGER-001` when it fails |
| `queuetruth ledger add --name NAME ...` | register a variant |

Filters: `--kind` (repeatable), `--study` and `--name` (globs), `--status ok|error|all`, `--since` and `--until`
(ISO 8601 date or time, UTC when no offset, or ns), `--dataset`, `--run-id` and `--spec-hash` (prefixes). Every
command takes `--ledger PATH` and the global `--format json`, `--quiet` and `--verbose`. The Python API has the same
reads: `list_records`, `n_trials`, `stats`, `variants`, `selection_inputs`, `export` and `verify_chain`, each taking a
`LedgerFilter`, a dict or keyword filters and an optional `ledger=` path. `queuetruth help ledger` prints the
command's help (the command is not listed in `queuetruth --help` yet).

<!-- check-docs: run -->
```bash
export QT_LEDGER_DIR="$PWD/ledger"
queuetruth ledger add --name carry --params '{"tenor": "3m"}' --dataset "FX G10 2023"
queuetruth ledger add --name carry --params '{"tenor": "6m"}' --dataset "FX G10 2023"
queuetruth ledger stats
queuetruth --format json ledger verify > verify.json
queuetruth ledger export --out ledger.parquet
```

## Tamper evidence

Each line of `ledger.jsonl` is the canonical JSON (RFC 8785) of `{"hash": H, "record": R}`, where `H` is the sha256
of the exact bytes of `R` in that line and `R.prev` is the `H` of the line before (64 zeros for the genesis record,
`seq` 0, which also fixes a random `ledger_id` that every record repeats). Every read checks every line's hash and
link before it returns anything, so a tampered ledger raises `QT-LEDGER-001` instead of returning a quietly wrong
trial count. `ledger verify` reports every problem by line:

| Reason | What happened |
|---|---|
| `hash_mismatch` | a record's bytes were edited |
| `broken_link` | a record was edited and re-hashed, or removed, inserted or moved |
| `seq_gap` | records were removed, repeated or reordered |
| `ledger_id_mismatch` | a record from another ledger was spliced in |
| `genesis` | the genesis record is missing or repeated |
| `not_canonical` | a record's bytes are not in canonical form (re-serialized by another tool) |
| `malformed`, `invalid_record` | a line is not a ledger line, or a record breaks the record schema |
| `anchor_mismatch` | the anchored record is no longer there |
| `series_missing`, `series_mismatch` | a series file was deleted, or edited (it no longer hashes to its name) |

A hash chain makes changes evident; it cannot stop someone who can write the file from rewriting all of it
consistently or from cutting off the newest records. To catch that, note the head (`ledger verify` prints it as
`seq:hash`) somewhere the ledger's owner cannot edit later (a commit, an email, a report, a timestamping service).
`ledger verify --anchor SEQ:HASH` then proves that the history up to that record is unchanged. The tests rewrite a
whole chain consistently and show that it passes on its own and fails against an earlier anchor
(`test_full_rewrite_passes_alone_but_fails_against_an_anchor`).

## Concurrency and crashes

An append takes an exclusive lock on `ledger.jsonl.lock` (POSIX `flock`, Windows `msvcrt.locking`, plus an
in-process lock for threads), reads only the last line, writes the new line with one `write` and `fsync`s. Parallel
runs therefore form one chain: the tests append from 4 processes and from 6 threads at once and check that the
chain verifies with contiguous `seq`s and each writer's records in its own order. A lock held longer than
`QT_LEDGER_LOCK_TIMEOUT_S` is `QT-LEDGER-002`, logged by the run, which still succeeds. `flock` is not reliable on
some network file systems; keep the ledger on a local disk.

A line is committed by its newline. Bytes after the last newline are an append that was cut off (a crash or a full
disk); reads ignore them, `verify` reports their length without failing, and the next append either commits them,
when they are a complete record that extends the chain and only the newline was lost, or moves them to `torn/` and
cuts them off. The next append refuses (`QT-LEDGER-001`) when the last committed line cannot be read at all, and when
the file is not a ledger (an existing file whose first line is not a ledger line is never overwritten). A child
process forked while another thread appends gets a fresh in-process lock, so it never waits on a lock nobody holds.

## Errors

| Code | When |
|---|---|
| `QT-LEDGER-001` | integrity: a record or series file altered, removed, inserted or unreadable, or a file that is not a ledger (`verify` details list every problem) |
| `QT-LEDGER-002` | the ledger cannot be created, opened, locked or appended to |
| `QT-INPUT-001` | no ledger at the location (nothing recorded there yet) |
| `QT-CONFIG-001` | an invalid record (a non-finite value in a series, an index that is not increasing, a name outside `[A-Za-z0-9_.:-]`, non-JSON params, a `genesis` record), filter or anchor, or a selection that spans studies or kinds |
| `QT-INPUT-002` | a series file with a value that is not a number |
| `QT-USAGE-001` | bad `ledger` command-line usage |

## Cost

Measured with `python/tests/ledger/bench_ledger.py` (run it from the repository root with `PYTHONPATH=python`) on an
Apple M1 Pro laptop in Low Power Mode, shared with other jobs (load average about 200, so absolute times are
inflated). Each row is the median of the stated number of runs, with the 90th percentile in brackets. The
10,000-record ledger holds a 252-value returns series for every record: 9.2 MB of chain plus 20.3 MB of series files.

| Operation | Runs | Time |
|---|---|---|
| append a variant without a series | 500 | 1.10 ms (1.38 ms) |
| append a variant with 252 returns | 500 | 1.83 ms (3.41 ms) |
| append a variant with 100,000 scores | 30 | 133 ms (213 ms) |
| `register_variants`: one batch of 100 variants with 252 returns | 20 | 318 ms (398 ms) |
| derive a `zoo` run's record, file hash already computed in the process | 500 | 0.81 ms (2.80 ms) |
| derive a `zoo` run's record, cold file hash of a 64 MiB shadows file | 10 | 93 ms (145 ms) |
| append to a ledger of 10,000 records (only the last line is read) | 100 | 1.05 ms (1.80 ms) |
| `n_trials` over 10,000 records (every line hash- and link-checked; no series read) | 5 | 0.95 s (1.37 s) |
| `list_records(limit=1)` over 10,000 records | 5 | 0.86 s (0.96 s) |
| `selection_inputs` over 10,000 records (10,000 series read and checked) | 5 | 3.5 s (3.9 s) |
| `ledger verify` of 10,000 records and their series files | 3 | 7.9 s (8.3 s) |
| `n_trials` over 30 records with 100,000-value series (28 KB of chain) | 5 | 0.90 ms (3.8 ms) |
| `selection_inputs` over those 30 records | 5 | 0.59 s (0.59 s) |

Recording a run hashes every file its spec names, as `run_id` does, and a fresh CLI process has no cache of the hash
the run itself just computed: about 1.5 ms per MiB of input file on this machine (the cold row above), on top of the
run. Reads verify the whole chain each time, so their cost grows with the number of records, not with the length of
the series, which stay in their own files.
