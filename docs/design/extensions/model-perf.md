# fit-hazard performance and safe model files (extension of SPEC 5.5.8 and 7.1)

Status: built and tested (`python/queuetruth/model/`, `python/tests/model/`). This note is the contract the code
was built against, written to be merged into [the design specification](../spec.md): section M.1 replaces the
`model.pkl` sentences of 5.5.8, M.2 adds to 5.5.6, and M.3 is the measurement for the `fit-hazard` row of 7.1.
M.5 lists the edits outside this workstream's files that must land in the same merge.

## M.1 Persistence (replaces the pickle in 5.5.8)

`<out>/model.npz` replaces `model.pkl`. It is a ZIP archive of uncompressed (`ZIP_STORED`) members with the fixed
timestamp 1980-01-01, so the same model gives the same bytes on every platform (7.2):

| Member | Content |
|---|---|
| `meta.json` | `qt.hazard_model_state`, `format_version` 2: spec echo, `feature_names`, `grid_ns`, `n_rows_train`, `estimator` (`family`, `fitted_with`: the scikit-learn version that fitted the trees, kept through load, migrate and re-save; `classes`, `n_iter`, `n_trees_per_iteration`, `n_features`), `isotonic` (grid indexes with a layer) |
| `baseline.npy` | float64 (1, K): the GBDT intercept per tree of an iteration |
| `tree_offsets.npy` | int64 (n_trees + 1): tree `t` is nodes `tree_offsets[t]` to `tree_offsets[t + 1]`, trees in iteration order then class |
| `node.<field>.npy` | one array per field of scikit-learn's tree node record: `value`, `count`, `feature_idx`, `num_threshold`, `missing_go_to_left`, `left`, `right`, `gain`, `depth`, `is_leaf`, `bin_threshold`, `is_categorical`, `bitset_idx` (fixed little-endian dtypes) |
| `iso.<j>.x.npy`, `iso.<j>.y.npy`, `iso.<j>.bounds.npy` | isotonic layer of grid point `j`: thresholds and clip range |

`model.json` (`qt.hazard_model_card`, unchanged schema) lists `model.npz` with its sha256 in `files`.

Reading never executes code. `HazardModel.load` reads the file once and at most 256 MiB (a default model is about
3 MB), checks its sha256 against the card (integrity) and against the caller's `sha256` when given (authenticity),
then accepts only stored members named `meta.json` (at most 1 MiB) or `<name>.npy`, parses each `.npy` (format 1.0
only) from its literal header with object dtypes refused and the payload length equal to `prod(shape) * itemsize`
before anything is allocated, and checks the state before any tree is walked: exact member set, dtypes and shapes;
`tree_offsets` increasing from 0; every split node's children after it and inside its tree (so every walk ends at a
leaf); split features inside the design; no categorical splits; no NaN threshold; finite leaf values and baseline;
classes increasing among 0, 1, 2 with 1 tree per iteration for two classes and K for K >= 3; isotonic thresholds
strictly increasing, values in [0, 1], clip range equal to the first and last threshold (scikit-learn's own
invariant; a wider range would clip inputs to points outside the interpolation table, where `predict` returns NaN),
layers only with `recalibration = isotonic`; `fitted_with` a string; feature names and grid equal to the card's. Any
failure, including a `meta.json` nested too deep to parse, is `QT-INPUT-002` naming it. scikit-learn's tree walker
does not bound-check node indices, which is why these checks come first.

The tree arrays are scikit-learn's own node record (`PREDICTOR_RECORD_DTYPE`) and prediction runs through its
private `TreePredictor` and loss link, so the format is coupled to scikit-learn (declared range `>=1.6,<2`, tested
with 1.9.1; a test at the minimum version is a follow-up). Two guards make a change in that coupling loud rather
than silent: saving refuses (`QT-ENGINE-001`) when the installed node record has a field the format does not store
or lacks one it does, and loading refuses the same mismatch; a model whose `fitted_with` differs from the installed
scikit-learn loads with a warning in `HazardModel.warnings`.

`trusted` does not apply to `model.npz` (nothing runs). Loading it without `sha256` succeeds, because the card's
digest is an integrity check, but whoever can write the directory can replace the model and its card together, so
the loaded model's `warnings` then say `model.npz not authenticated`. `model_sha256(card)` returns the digest of
`model.npz` (else of a legacy `model.pkl`); `pickle_sha256` is kept as its v0.1 name.

Legacy v0.1 directories (`model.pkl` listed in the card): `HazardModel.load` refuses them with `QT-CONFIG-001`
(`details.field = allow_pickle`) unless `allow_pickle=True`; then the v0.1 rule still applies (a trusted `sha256` of
`model.pkl` or `trusted=True`, else `QT-CONFIG-001`; a digest mismatch is `QT-INPUT-002` before unpickling), the
bytes hashed are the bytes unpickled, `LegacyPickleWarning` (a `UserWarning`) is emitted and the model's
`warnings` say so; the scikit-learn version that pickled it (from scikit-learn's own `InconsistentVersionWarning`,
else the installed one) becomes `fitted_with`. `python -m queuetruth.model.migrate DIR --allow-pickle --sha256
<digest>` (or `--trusted`) converts a directory in place (`model.pkl` stays on disk but is no longer listed) or into
`--out`; with `--out`, every listed name and digest is checked before the output directory is created or anything
is copied. `DIR` must exist (a mistyped path is `QT-INPUT-001` naming it, never its parent). Output follows the CLI
convention of SPEC 4.4: by default stdout is the new `model.npz` digest and stderr the summary or the error message
and hint; with `--format json` stdout is exactly one document, `{"model", "sha256", "legacy_pickle_left"}` or a
`qt.error`; exit 0 or 2. Without `--allow-pickle` nothing is unpickled.



## M.2 Prediction and fit (adds to 5.5.6)

- The fitted GBDT is held as a `TreeEnsemble`: scikit-learn's own `TreePredictor` per tree and its own loss link
  (`HalfMultinomialLoss`, or `HalfBinomialLoss` when the train split has no cancel), so its probabilities equal
  `HistGradientBoostingClassifier.predict_proba` bit for bit. It predicts in blocks of 4,096 rows on a thread pool
  (one OpenMP thread per call, `OMP_NUM_THREADS` or the CPU count workers): every row is the same sum over the trees
  in the same order whatever the block or thread count. `HazardModel.estimator` is this `TreeEnsemble` (it was the
  scikit-learn estimator in v0.1); `HazardModel.sklearn_estimator` keeps the scikit-learn object of a fit in this
  process (None after `load`).
- Each kept row of the split table is predicted at most once per fit and cached. `fit` predicts the calib and test
  rows (the isotonic layers and the test scores need them); `predict_split()`, which writes `predictions.parquet`,
  adds the train rows only then, so `fit_hazard` with `out` null predicts no train row (v0.1 predicted the calib
  rows in `fit` and the test rows again in scoring). `split_arrays(mask)` and `predict_split()` are in-process
  helpers of a fitted model (`QT-USAGE-001` after `load`). Every step after the hazards is row-wise, so a slice of
  the cache equals predicting the slice again (tested bit for bit, as is the number of rows each call predicts).
- `fit` resets the model first, and a fit that raises leaves it unfitted, so no cache or estimator of an earlier
  fit survives a failed refit.
- The GBDT fit runs under `fit_threads(threads)` OpenMP threads, an execution-only choice that does not change the
  trees (scikit-learn's histogram GBDT is deterministic across thread counts; tested at 1 and 4 threads, with the
  limit checked to reach scikit-learn's OpenMP code). In order: `HazardModel.fit(..., threads=n)`; else
  `OMP_NUM_THREADS`; else, when the process is confined to fewer logical CPUs than the host has (CPU affinity or a
  cgroup quota, as scikit-learn counts them), scikit-learn's own count, because the host-wide load average says
  nothing about those CPUs; else `os.cpu_count()` minus the 1-minute load average, at least 1 and at most
  scikit-learn's count (scikit-learn's count on a platform without a load average). The count used is
  `HazardModel.fit_threads_used`. Two limits of the load rule: the load average lags by about a minute, so fits
  started together on an idle machine can still oversubscribe it; and it counts this process's own recent work, so
  right after a multi-threaded stage (features, zoo) on a quiet machine the fit may take fewer threads than are
  free. `threads=` or `OMP_NUM_THREADS` fixes the count in both cases. `threadpoolctl` (a scikit-learn dependency)
  applies the limit.
- `zoo_comparison` is vectorised (one planned-life lookup per distinct shadow id, `numpy.unique` for the first
  eligible truth and the last eligible prediction per key). The per-model sums run over the same rows in the same
  order as the v0.1 loop, so every Brier value and every warning is unchanged.

## M.3 Measurement (7.1, `fit-hazard`)

What was met: the CPU time of a one-thread `fit-hazard` on 66,460 real order-landmarks is 61.8 s to 68.6 s against the
120 s target, so the target holds whenever the fit has one core to itself. What was not measured: the multi-thread
fit on a quiet machine (the load rule of M.2 picks up to 8 threads there), and the bench case of 7.1
(`ana.real_day1.fit_hazard_60k`, through the CLI, 60,000 order-landmarks of day one, no zoo comparison), whose
prepared inputs (`.realdata/bench`) do not exist yet. That case is the next measurement to take, with `make bench`.

Workload: the RD-10 L2-only fit on real Nasdaq ITCH, `.realdata/features/2d-40000/features.parquet` (66,460
order-landmarks, 38,712 orders, two days; 152,535 train person-period rows x 36 design columns), default
`HazardFitSpec` (300 iterations x 3 trees, isotonic, `bootstrap_b` 1,000) plus the zoo comparison on
`.realdata/zoo/2d-40000-subset` (2,000,000 zoo rows), `out` set. In-process `fit_hazard(spec)`, wall and process
CPU time. Machine: Apple M1 Pro, 8 cores, 16 GB, Low Power Mode on, shared with many other agents (1-minute load
average 39 to 460 during the runs, so every after run took 1 thread), every run at `nice 10`. Before = the `main`
checkout (dd6f8c7), after = this change, on 2026-10-24:

| Run | Load (start, end) | Wall | Process CPU |
|---|---|---|---|
| before, under cProfile | about 250 | 818.0 s | 166.7 s |
| before | 321, 428 | 441.1 s | 162.9 s |
| before | 280, 179 | 1,876.9 s | 158.1 s |
| before, `OMP_NUM_THREADS=1` | 226, 208 | 97.3 s | 77.2 s |
| after | 222, 335 | 121.7 s | 68.6 s |
| after | 426, 457 | 103.0 s | 65.0 s |
| after, under cProfile | about 230 | 90.2 s | 66.6 s |
| after | 254, 266 | 133.2 s | 67.7 s |
| after | 178, 223 | 65.1 s | 62.5 s |
| after | 214, 226 | 61.1 s | 62.6 s |
| after (review reproduction) | 97, 227 | 151.8 s | 67.3 s |
| after, final code | 43, 49 | 53.3 s | 61.8 s |
| after, final code | 41, 39 | 56.7 s | 63.3 s |

The like-for-like code gain is the one-thread comparison: v0.1 with `OMP_NUM_THREADS=1` took 77.2 s of CPU, this
change 61.8 s to 68.6 s (11% to 20% less), from predicting each row once and the vectorised zoo comparison. That is
the gain a user on a quiet machine should expect from the code itself.

Wall time follows the load. Over the 8 plain after runs (the cProfile run left out) the median is 84.1 s and the
range 53.3 s to 151.8 s; 3 of the 8 (121.7 s, 133.2 s, 151.8 s) missed 120 s, each while the machine was loaded
enough that the one-thread fit got about half a core (wall / CPU 1.8 to 2.3). The before runs as shipped (441 s to
1,877 s wall, 158 s to 167 s CPU) show the oversubscription pathology on a loaded machine rather than the speed of
the code: v0.1 started 8 OpenMP threads whatever the load, which cost about twice the CPU and up to 19x the wall of
its own one-thread run.

Where the time went (cProfile, wall under load): before, the GBDT fit 690 s (8 OpenMP threads: 27,000 short
parallel regions in `split_next` waited on descheduled threads, 491 s), 7 `predict_proba` calls 111 s, the
zoo-comparison Python loop 12.5 s; after, the GBDT fit 76 s on 1 thread, one prediction of all rows 10 s, test
scores 2.3 s, zoo comparison 0.8 s. A separate fit of the same train design under load 180 to 270 took 82.7 s wall
on 1 thread, 161.5 s on 2 and 183.6 s on 4.

Equivalence on this workload: `predictions.parquet` of every before and after run, the final code included, is
byte-identical (sha256 `4db8db24...`, 13,830,400 bytes), and `model.json` is identical except the `files` entry of
the model file. The `model.npz` of every after run of the second session and of the final code is byte-identical
(sha256 `336924ba...`, 3,100,944 bytes); the v0.1 `model.pkl` was not (two runs, two digests, 3,231,224 bytes
each). `HazardModel.load` takes 16 to 39 ms for `model.npz` (35 to 44 ms for the v0.1 pickle). The v0.1 pickle of
`.realdata/hazard/2d-40000-l2` migrates to a `model.npz` whose predictions equal the pickle's and its stored
`predictions.parquet` bit for bit (`test_real_v01_pickle_migrates_to_identical_predictions`, opt-in with
`QT_REALDATA_DIR`).

## M.4 Tests

`python/tests/model/test_model_perf_equivalence.py` (bit-for-bit: `TreeEnsemble` vs `predict_proba`, multiclass and
binary, missing values, 1 and 3 threads, three blocks; cached hazards vs the v0.1 chunked path; isotonic thresholds
vs v0.1; `split_arrays` / `predict_split` vs fresh prediction; each row predicted at most once and train rows only
for `predict_split`, also through `fit_hazard` without `out`; a failed refit leaves the model unfitted; the fit at 1
and 4 threads gives byte-identical `model.npz` with the thread limit checked inside scikit-learn; the thread policy,
including confinement, a missing load average and invalid counts; the vectorised `zoo_comparison` vs the verbatim
v0.1 loop on randomised tables with duplicate rows, null and NaN values, off-grid horizons, unknown shadows, a model
predicting a subset and timestamps near the int64 limit), `test_model_persist.py` (format determinism and numpy
compatibility, 30 crafted states and 12 crafted archives refused, the size limit, the node-record guard on save and
load, the authenticity and fitting-version warnings, `fitted_with` kept through re-save and the legacy path, hostile
pickles never run without the opt-in and a matching digest, `migrate` end to end with its output convention and its
checks before anything is written, an interrupted save leaves no file) and the updated `test_model_hazard.py` /
`test_model_card.py` (round trip, tamper, determinism across `OMP_NUM_THREADS`).

## M.5 Integration (edits outside this workstream's files, same merge)

These files are not owned by this workstream; without the edits below the merge breaks `make examples` and
`docs/check_docs.py`, and leaves pages describing `model.pkl`.

1. `examples/06_learned_model.py`, function `predict`: replace
   `sha = next(f.sha256 for f in card.files if f.name == "model.pkl")` (it raises `StopIteration` now) with
   `sha = qt.model.model_sha256(card)`, and its docstring's second sentence with "`load` checks `model.npz`
   against the trusted sha256 `fit_hazard` recorded in the card it returned (no code runs when loading)."
2. `docs/reference/python-api.md`: regenerate with `python docs/gen_reference.py` (`HazardModel.load`, `fit`,
   `fit_hazard`'s docstring and `queuetruth.model.__all__` changed).
3. `docs/mkdocs.yml` nav: add this page, or merge M.1 to M.3 into the spec as in items 4 to 6 and drop the page.
4. `docs/design/spec.md` 4.3 (lines 1413 to 1415): the signature becomes
   `qt.HazardModel.load(path: str, *, sha256: str | None = None, trusted: bool = False, allow_pickle: bool = False)`
   with the comment "model.npz: no code runs; sha256 checked against the card and against `sha256` when given
   (QT-INPUT-002), warning when not given; legacy model.pkl: QT-CONFIG-001 unless allow_pickle=True, then a trusted
   sha256 or trusted=True as in v0.1".
5. `docs/design/spec.md` 5.5.8 (lines 2375 to 2379): replace "`model.pkl` (pickled estimator + isotonic layers; ...
   the service never unpickles user data)" with "`model.npz` (trees and isotonic layers as `.npy` arrays plus a JSON
   header, read without executing code and checked before use, extension model-perf M.1; its sha256 is recorded in
   the card; `HazardModel.load` checks it, and the caller's trusted digest `model_sha256(card)` when given; a legacy
   `model.pkl` loads only with `allow_pickle=True`; the service never loads user models)". Add the M.2 bullets to
   5.5.6.
6. `docs/design/spec.md` 7.1 (line 3046): replace "`fit-hazard` 172 s (missed, 120 s)" with "`fit-hazard` 172 s
   (missed, 120 s; after extension model-perf one-thread CPU 61.8 s to 68.6 s on 66,460 real order-landmarks, wall
   under load 53 s to 152 s; the bench case is not re-measured)". 8.11 (line 3302): replace "`HazardModel.load` needs
   a trusted digest" with "`HazardModel.load` never unpickles without `allow_pickle=True` and a trusted digest".
7. `SECURITY.md` (lines 38 and 39): replace the bullet with "Loading a legacy `model.pkl` with
   `HazardModel.load(..., allow_pickle=True, trusted=True)` from a directory someone else can write. `model.npz`
   never executes code; without `allow_pickle=True` a pickle is never loaded."
8. `docs/concepts/learned-model.md`, section "Using it": `fit-hazard` writes `model.npz`, not `model.pkl`; the
   example imports `model_sha256` (not `pickle_sha256`) and the text says that `model.npz` loads without running
   code, that `sha256` makes the load authenticated (without it `warnings` says so), and that a v0.1 directory is
   converted once with `python -m queuetruth.model.migrate DIR --allow-pickle --sha256 <digest>`.
9. `docs/data-and-licensing.md` line 45: `model.pkl` becomes `model.npz`.
10. `docs/reference/errors.md` line 13 (`QT-INPUT-002` examples): "a model pickle whose sha256 does not match"
    becomes "a model file whose sha256 does not match, or a crafted or damaged `model.npz`".
11. `service/tests/security/test_low_findings.py`, `test_l6_model_loader_checks_the_sha256_before_unpickling`
    (line 218): it now passes because the `allow_pickle` gate refuses first, so the digest check before unpickling
    is no longer exercised. Call `HazardModel.load(str(d), allow_pickle=True, trusted=True)` there (the card's
    digest does not match, so it must still refuse with "sha256" and leave no marker), and keep the second L6 test
    as it is.
12. `pyproject.toml`: declare `threadpoolctl>=3.1` (imported directly by `queuetruth.model.hazard`; today it
    arrives only through scikit-learn; `uv.lock` already holds it).
13. CLI and spec (later, when the CLI hub is next touched): an execution-only `threads` field on `HazardFitSpec`
    and `queuetruth fit-hazard --threads`, passed to `HazardModel.fit(threads=)` and kept out of `run_id` like
    the other `threads` fields; and `queuetruth model migrate` as a subcommand wrapping
    `queuetruth.model.migrate.migrate`.
