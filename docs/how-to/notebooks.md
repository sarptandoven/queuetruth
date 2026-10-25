# Use queuetruth from a notebook

**Goal:** explore replays, zoo leaderboards, calibrations and scorecards interactively, as pandas or polars
DataFrames, starting from one of four executed example notebooks.

**You need:** `pip install queuetruth` plus pandas (or polars) and, for the plots, matplotlib. Colab has pandas,
polars and matplotlib preinstalled; the setup cell installs queuetruth. `queuetruth.frames` ships in the release after 0.1.1; with 0.1.1 the notebooks stop at their setup
cell with a message saying so.

## The example notebooks

The notebooks live in `examples/notebooks/` and are stored with their outputs, so you can read the results on
GitHub without running anything. Each starts with an "Open in Colab" badge and a setup cell that installs
queuetruth from PyPI when it is missing.

| Notebook | Data | What it shows |
|---|---|---|
| [`01_quickstart.ipynb`](https://github.com/sarptandoven/queuetruth/blob/main/examples/notebooks/01_quickstart.ipynb) | synthetic | exact replay with the invariant and parity checks; 1,000 shadow orders in the real queue and their wait by queue ahead; five L2 models against exact truth with bootstrap intervals; the bias of NautilusTrader's touch model by queue decile; one fill explained event by event; `qt.Session` |
| [`02_nautilus_too_optimistic.ipynb`](https://github.com/sarptandoven/queuetruth/blob/main/examples/notebooks/02_nautilus_too_optimistic.ipynb) | two public Nasdaq ITCH sample days (stored outputs), or synthetic | NautilusTrader's default `FillModel` and `queue_position=True` against exact replay on 3,000 shadows per day; which orders the default fills wrongly; fitting `prob_fill_on_limit` on one day, testing it on the other and exporting a `FillModel` line |
| [`03_calibrate_and_export.ipynb`](https://github.com/sarptandoven/queuetruth/blob/main/examples/notebooks/03_calibrate_and_export.ipynb) | synthetic | fitting hftbacktest's `power3` exponent and NautilusTrader's touch probability against exact truth; the held-out comparison with the library default; the fail-safe export, its `QT-CALIB-001` refusals and `force`; the cancel evidence behind a fit |
| [`04_score_simulator_vs_fills.ipynb`](https://github.com/sarptandoven/queuetruth/blob/main/examples/notebooks/04_score_simulator_vs_fills.ipynb) | synthetic, with a synthetic live log | a naive "fill on touch" simulator written in pandas, a stricter variant, a zoo model and the exact arm, each scored against the live fills; which orders disagree and how their fills mark out; a CI gate on the bias |

What the stored outputs show, measured when they were executed:

- Notebook 02, 3,000 shadows per Nasdaq day: NautilusTrader's default touch model (`nt_prob_touch[p=1]`)
  overfills by +3.13 pp at 1 s on 2019-12-30 (95% CI +2.63 to +3.64) and by +2.80 pp on 2020-01-30 (+2.15 to
  +3.55), the same figures and intervals as the faithful-mode table in [Results](../RESULTS.md). `nt_queue_l2`
  stays within 0.77 pp of exact at 1, 10 and 60 s on both days. Every model is scored on all 3,000 shadows of its
  day: the notebook runs no hftbacktest model next to the NautilusTrader ones, because a faithful hftbacktest abort
  (its issue #312) removes that shadow from every model's row. The fitted touch probability is `p = 0.287`
  (bootstrap CI 0.273 to 0.304), which beats `p = 1` on the held-out day at every horizon and cuts the 1 s bias
  there from +2.80 pp (the default's held-out row, the same shadows as the zoo table) to +0.41 pp.
- Notebook 04: the naive touch simulator overfills the synthetic live log by 9.0 pp within 60 s (CI +5.5 to
  +12.5), the strict variant underfills by 18.0 pp, and the exact arm agrees with every order.

The real-data notebook publishes only aggregate statistics (fill rates, biases, counts, a fitted parameter), never
prices or order-level rows, and names its source, as [Data and licensing](../data-and-licensing.md) requires. Point
it at your own data with the `QT_NB_DATASET` environment variable: an order-level dataset directory with at least
two session dates. In a running Jupyter or Colab kernel, run `%env QT_NB_DATASET=/path/to/dataset` before the
first code cell. `queuetruth ingest` writes one session date per run, so build a two-day dataset from two ingests
with the repository's `scripts/realdata/merge_datasets.py`. For the two public Nasdaq sample days (files from
[emi.nasdaq.com/ITCH](https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/)):

<!-- check-docs: skip (needs the two Nasdaq ITCH sample files and a repository checkout) -->
```sh
queuetruth ingest --format itch --in 12302019.NASDAQ_ITCH50.gz --venue nasdaq --date 2019-12-30 \
    --symbols AAPL,MSFT,SPY,TSLA --out nasdaq-20191230
queuetruth ingest --format itch --in 01302020.NASDAQ_ITCH50.gz --venue nasdaq --date 2020-01-30 \
    --symbols AAPL,MSFT,SPY,TSLA --out nasdaq-20200130
python scripts/realdata/merge_datasets.py --out nasdaq-2d nasdaq-20191230 nasdaq-20200130
```

The stored outputs used an ingest of the same days from the order-level CSVs of an earlier prototype. Notebook 02 on
the dataset these three commands build (run on four-symbol extracts of the two files) gives the same exact fill
rates, bias table, McNemar counts and fitted `p` to the printed digit.

## Load run outputs as DataFrames

Every queuetruth command writes a summary document and Parquet tables. `queuetruth.frames` reads them into pandas
or polars, whichever is installed (pandas first). Make a small zoo run to work with:

<!-- check-docs: run -->
```sh
queuetruth fixtures synth --out synth
queuetruth shadows sample --data synth --n 300 --out shadows.parquet
queuetruth zoo --data synth --shadows shadows.parquet --out zoo --models "hbt_power3[n=2],nt_prob_touch[p=1]"
```

`frames.load_run` opens a run directory. The result is a read-only mapping from table name to DataFrame, read on
first use; it also carries the summary (`document`, `schema`, `run_id`). Its tables are the Parquet files the
summary lists, plus those listed by nested run summaries (the `held_out/summary.json` of a calibration); a stray
`.parquet` file that no summary lists is not a table of the run, and `unlisted` names it. A listed name outside the
run directory is `QT-INPUT-002`. `verify=True` checks every listed file against the sha256, size and row count its
summary records, and raises `QT-INPUT-002` naming each file that differs (`QT-INPUT-001` when a listed file is
missing or unreadable). Some outputs carry no file manifest: `calibration.json` lists no files, so a calibration's
`alloc_events` and `touch_records` are opened but cannot be verified, and `verify()` says so with a `UserWarning`
(as it does when nothing at all could be checked).

<!-- check-docs: run -->
```python
from queuetruth import frames

run = frames.load_run("zoo", verify=True)
print(run.schema, run.run_id, list(run))
errors = run["zoo_errors"]
print(len(errors), "rows in zoo_errors")
```

The nested summaries flatten into tidy tables with fixed columns: `leaderboard` (one row per model and horizon),
`held_out` (calibration: one row per arm, model and horizon, with the trust check's `beats_default`) and
`scorecards` (one row per scorecard, for comparing simulators side by side). They accept the Pydantic summary a
Python call returns, its dict, a run directory or the summary file.

<!-- check-docs: run -->
```python
from queuetruth import frames

board = frames.leaderboard("zoo")
one_second = board[board["horizon_ns"] == 1_000_000_000]
print(one_second[["model_id", "fill_rate_bias", "ci_lo", "ci_hi", "exact_fill_rate"]].to_string(index=False))
```

Single tables and summary lists:

<!-- check-docs: run -->
```python
from queuetruth import frames

shadows = frames.read("shadows.parquet", expect="qt.shadow_spec", columns=["shadow_id", "price", "qty"])
print(shadows.head(3).to_string(index=False))
lb = frames.records("zoo", "leaderboard")
print(sorted(c for c in lb.columns if c.startswith("overfill_bias")))
```

| Function | Returns |
|---|---|
| `load_run(path, backend=None, verify=False)` | `RunFrames`: table name to DataFrame (`read(name, columns, filters)` for part of a table, `table(name)` for the `pyarrow.Table`, `file_path(name)`, `model()` for the validated Pydantic summary, `verify()`, `unlisted`) |
| `read(path, expect=None, columns=None, backend=None, filters=None)` | one table; `expect` checks its `qt.schema`; `columns` and `filters` (pyarrow's form, `[("instrument_id", "==", 13)]`) are pushed down to the Parquet reader, so a few columns of a full-day table cost only those columns |
| `to_frame(table, backend=None)` | a `pyarrow.Table`, record batch or list of row dicts as a DataFrame |
| `leaderboard(zoo)` | `model_id`, `horizon_ns`, `fill_rate_bias`, `ci_lo`, `ci_hi`, `exact_fill_rate`, `model_fill_rate`, `brier`, `logloss`, `overfill_bias`, `qty_bias`, `mcnemar_b`, `mcnemar_c`, `mcnemar_p_exact`, and the scoring population shared by every model: `n_leaderboard` and `n_excluded_hbt_312` (shadows dropped from every row because a faithful hftbacktest model aborted on them) |
| `held_out(calibration)` | `arm` (`fitted` or `default`), `model_id`, `horizon_ns`, `brier`, `logloss`, `fill_rate_bias`, `beats_default` |
| `scorecards(scores)` | one row per scorecard (a mapping gives the `label`): the 2x2 table, `fill_rate_bias` with CI and `direction`, `qty_bias`, McNemar, `discordance_p`, `gates_passed`, `headline` |
| `records(doc, field)` | any list field of a summary (`markouts`, `by_queue_decile`, `stratified`, ...), nested objects as dotted columns; the columns are the union over all elements, null where an element lacks one |

Biases and rates are fractions, not percentage points. Integer values are never rounded: on pandas, integer and
boolean columns use the nullable dtypes (`Int64`, `UInt64`, `boolean`), so a null never turns an int64 price or id
into a float, and list, struct and map columns become `pandas.ArrowDtype` columns that keep their element types.
Timestamps stay `datetime64[ns, UTC]` and dictionary columns become categoricals. Float columns stay `float64`, where
a null reads as NaN, so on pandas the two cannot be told apart (use `run.table(name)` or polars when that matters).
Polars keeps every Arrow type.

Choose the library with `backend="pandas"` or `backend="polars"`, or for a whole session with the environment
variable `QT_FRAMES_BACKEND`. An unknown name is `QT-CONFIG-001`; a library that is not installed is
`QT-USAGE-001` with the `pip install` line to fix it.

## Rebuild the notebooks

The notebooks are generated from the percent-format scripts in `examples/notebooks/src/` by
`examples/notebooks/build.py`, which adds the badge and the setup cell. Executing them needs `nbclient`, `nbformat`
and `ipykernel`, which are not queuetruth dependencies, so use a throwaway virtual environment:

<!-- check-docs: skip (needs a separate virtual environment and network access) -->
```sh
uv venv .scratch/nbvenv
uv pip install --python .scratch/nbvenv/bin/python nbclient nbformat ipykernel pandas matplotlib \
    pyarrow numpy scipy scikit-learn pydantic
PYTHONPATH=$PWD/python .scratch/nbvenv/bin/python examples/notebooks/build.py --execute --workdir .scratch/nbrun
```

The kernel is an ipykernel started by the interpreter that runs `build.py` (a `python3` kernelspec installed
elsewhere cannot substitute another Python), and it runs in `--workdir`, so outputs never land in the repository
(and `PYTHONPATH` must be an absolute path). The build fails if a cell raises, if any output contains a local path
(the repository, the working directory or the home directory), or if a notebook is larger than `--max-kb` (default
200). One of `--execute` and `--no-outputs` is required, so a plain run never replaces the stored outputs with empty
ones. The stored notebooks are 44,913 to 56,938 bytes each. To reproduce the stored outputs of notebook 02, set
`QT_NB_DATASET` to the two-day Nasdaq dataset built above; without it, notebook 02 runs on synthetic data and labels
every result `SYNTHETIC`.
