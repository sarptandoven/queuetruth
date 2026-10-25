# Extension: `queuetruth.frames` and the example notebooks

Status: built (workstream `notebooks`). Additive: no schema, spec or run_id change.

## `queuetruth.frames` (Python only)

A convenience layer over the Python API (SPEC 4.3) for notebooks. It never writes files and never changes a run's
outputs, so it has no run_id of its own.

- Backend: `backend` argument, else env `QT_FRAMES_BACKEND`, else `auto` = pandas if importable, else polars.
  Values `auto`, `pandas`, `polars` (case-insensitive, trimmed); anything else is `QT-CONFIG-001` naming the source
  (`backend` or `QT_FRAMES_BACKEND`); a requested or auto backend that is not installed is `QT-USAGE-001` with a
  `pip install` hint. Neither library becomes a dependency (pandas is already a dev dependency; polars is the
  existing `queuetruth[polars]` extra).
- `to_frame(data, backend=None)`: `pyarrow.Table`, `RecordBatch` or a sequence of row mappings (else
  `QT-INPUT-002`). Row mappings: the columns are the union of every row's keys in first-seen order (null where a
  row lacks one); values one column cannot hold together are `QT-INPUT-002`. pandas: `Table.to_pandas` with integer
  and boolean Arrow types mapped to the pandas nullable extension dtypes (`Int8` .. `UInt64`, `boolean`) and list,
  large list, fixed-size list, struct and map types to `pandas.ArrowDtype`, so integers are never rounded (no int64
  to float64 on nulls, also inside lists); float columns stay `float64` (a null reads as NaN); timestamps
  `datetime64[ns, tz]`; dictionary columns categorical; `DataFrame.attrs["qt.schema"]` = the table's `qt.schema`
  metadata when present. polars: `polars.from_arrow`.
- `read(path, expect=None, columns=None, backend=None, filters=None)`: without `columns` and `filters`,
  `queuetruth.api.read_table` (so `qt.schema` and, for core tables, the columns are checked). With either, the
  footer schema is read first (`qt.schema` present and equal to `expect`, every named column present, else
  `QT-INPUT-002`), then `pyarrow.parquet.read_table(columns=..., filters=...)` reads only those columns and the row
  groups that can match; an unusable filter is `QT-INPUT-002`. Columns come back in the order asked for.
- `load_run(path, backend=None, verify=False)` -> `RunFrames`. `path` is a run directory or its summary JSON file
  (a `.parquet` file is `QT-INPUT-002` with a hint to use `read`; a missing path `QT-INPUT-001`). The summary is the
  first present of `summary.json`, `calibration.json`, `model.json`, `referee_report.json`, `dq_report.json`,
  `manifest.json`; it must be a JSON object with a `qt.*` `schema` field (`QT-INPUT-002`). It is not validated on
  load (forward compatibility); `RunFrames.model()` validates it with `queuetruth.schemas.parse_document`
  (`QT-INPUT-002` on failure). Listed files: the summary's `files` plus the `files` of every nested `qt.*`
  `summary.json` below the directory (a calibration's `held_out/summary.json`), keyed by POSIX path relative to the
  run directory. A name that is empty, absolute, has a drive, contains `..` or resolves outside the run directory is
  `QT-INPUT-002`; a listed file missing on disk is `QT-INPUT-001` at load. Tables: when the top summary lists files,
  exactly the listed `.parquet` files; when it lists none (`calibration.json`), every `*.parquet` under the
  directory. Parquet files no summary lists are `RunFrames.unlisted`. Keys are relative POSIX paths without the
  suffix, sorted. Tables are read on first access and cached per `RunFrames`; `RunFrames.read(name, columns,
  filters)` reads part of one, uncached, as `read` does.
- `RunFrames.verify()`: for every listed file, compares `bytes`, then `sha256`, then (Parquet) `rows` with the
  Parquet footer; all mismatches are reported in one `QT-INPUT-002`, and a listed file that is missing or cannot be
  read is `QT-INPUT-001`. Returns the number of files checked. A `UserWarning` says when nothing could be checked
  (no listing anywhere) and when `unlisted` is not empty (ignored stray files, or tables such as a calibration's
  `alloc_events` that no manifest covers).
- Flattened summaries, each with a fixed Arrow schema so empty inputs keep their columns and both backends agree:
  - `leaderboard(zoo)`: one row per leaderboard entry and horizon (union of the entry's horizon maps, ascending);
    columns `model_id, horizon_ns, fill_rate_bias, ci_lo, ci_hi, exact_fill_rate, model_fill_rate, brier, logloss,
    overfill_bias, qty_bias, mcnemar_b, mcnemar_c, mcnemar_p_exact, n_leaderboard, n_excluded_hbt_312` (the last two
    are the run's scoring population, repeated on every row: null and 0 when the summary predates them).
  - `held_out(calibration)`: rows for `held_out` (`arm = fitted`) then `baseline_held_out` (`arm = default`), models
    sorted, horizons ascending; `beats_default` from `trust.beats_default` (null without a trust block).
  - `scorecards(scores)`: one scorecard, a sequence, or a mapping label -> scorecard; `label` = key, else
    `sim_source.model_id`, else `sim_source.kind`; `discordance_p` = `discordance_clustered.p_value`.
  - Inputs may be the Pydantic summary, its dict, a `RunFrames`, a run directory or a summary path; a document of
    another schema is `QT-INPUT-002`.
- `records(doc, field)`: a top-level list-of-objects field as rows, nested objects flattened to dotted names. The
  columns are the union of the leaf paths over all elements in first-seen order; an object that is null or absent
  in one element gives nulls in its columns there. An absent or non-list field, non-object items, a key that is an
  object in one element and a non-null value in another, two paths with the same dotted name, or values one column
  cannot hold together are `QT-INPUT-002`.

## Example notebooks

`examples/notebooks/NN_name.ipynb` are generated from `examples/notebooks/src/NN_name.py` (percent format) by
`examples/notebooks/build.py`, which inserts the Colab badge and a setup cell (installs `queuetruth`, pandas and
matplotlib from PyPI when queuetruth is missing, through `%pip` under IPython so Colab can offer a restart, then
`importlib.invalidate_caches()`; raises when `queuetruth.frames` is still missing). One of `--execute` and
`--no-outputs` is required. `--execute` runs them with nbclient in a separate working directory, with an ipykernel
launched by `sys.executable` (a kernel spec manager without kernel directories, so an installed `python3`
kernelspec cannot substitute another interpreter), and rejects a notebook with an error output, an output containing
the repository, working or home directory path, or a size above `--max-kb` (200). Execution timing metadata is not
stored. Notebook 02 reads `QT_NB_DATASET` (an ingested order-level dataset with at least two session dates) and
falls back to the synthetic dataset with every result labelled `SYNTHETIC`; on real data it shows aggregate
statistics only (docs/data-and-licensing.md). It assigns each shadow to the session (`instruments.parquet`
`midnight_utc_ns + open_s .. close_s` of its instrument) containing its submit time, asserting exactly one, so
sessions that cross UTC midnight split correctly. Its zoo runs hold only NautilusTrader models and the two bounds,
so `n_excluded_hbt_312 = 0` (asserted) and every row is scored on all shadows of the day.
