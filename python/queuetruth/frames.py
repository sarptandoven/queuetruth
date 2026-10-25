"""Run outputs as pandas or polars DataFrames, for notebooks and ad-hoc analysis.

queuetruth itself returns `pyarrow.Table`s and Pydantic summaries and depends on neither pandas nor polars. This
module converts them into whichever DataFrame library is installed:

- `to_frame(table)` converts one Arrow table (or a list of row dicts);
- `read(path, expect=...)` reads one queuetruth Parquet table (its `qt.schema` metadata is checked, as in
  `qt.read_table`);
- `load_run(out_dir)` opens a run output directory (`replay`, `zoo`, `calibrate`, `score`, ...): its summary
  document, its run id, and its Parquet tables as lazily read, cached frames (`run["zoo_errors"]`), with an
  optional integrity check of the sha256, size and row count the summary (and any nested run summary) records for
  each file;
- `leaderboard(zoo)`, `held_out(calibration)` and `scorecards(...)` flatten the nested summary documents into tidy
  long tables, one row per model and horizon (or per scorecard), with the same columns on both backends;
- `records(doc, field)` turns any list-of-objects field of a summary (`markouts`, `by_queue_decile`, ...) into a
  frame.

Backend choice: the `backend` argument (`"pandas"`, `"polars"` or `"auto"`), else env `QT_FRAMES_BACKEND`, else
`"auto"`, which takes pandas when it is importable and polars otherwise. Neither installed is `QT-USAGE-001`.

Integer values are never rounded. On pandas, integer and boolean columns become the nullable extension dtypes
(`Int64`, `UInt64`, `boolean`, ...), so a null never turns an int64 price, id or nanosecond count into a lossy
float64; list, struct and map columns become `pandas.ArrowDtype` columns (their elements keep their Arrow types);
timestamps stay `datetime64[ns, UTC]`, dictionary columns become categoricals, and `DataFrame.attrs["qt.schema"]`
records the table's schema name. Float columns stay `float64`, where a null reads as NaN (the two cannot be told
apart on pandas; use `table()` or polars when that matters). On polars, `polars.from_arrow` keeps every Arrow type.
Row order is the file's order for tables and a fixed order (summary order, then horizon ascending) for the
flattened summaries.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import warnings
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from queuetruth.errors import QtError
from queuetruth.schemas.io import sha256_file
from queuetruth.schemas.models import QtModel

__all__ = [
    "BACKENDS",
    "ENV_BACKEND",
    "SUMMARY_FILES",
    "RunFrames",
    "available_backends",
    "held_out",
    "leaderboard",
    "load_run",
    "read",
    "records",
    "resolve_backend",
    "scorecards",
    "to_frame",
]

#: Supported DataFrame libraries, in the order `"auto"` tries them.
BACKENDS: tuple[str, ...] = ("pandas", "polars")
#: Environment variable that sets the backend when no `backend` argument is given.
ENV_BACKEND = "QT_FRAMES_BACKEND"
#: Summary document file names of a run output directory, in lookup order (SPEC 5.1.6, 5.4, 5.5.8, 5.7, 5.8, 1.2).
SUMMARY_FILES: tuple[str, ...] = (
    "summary.json",
    "calibration.json",
    "model.json",
    "referee_report.json",
    "dq_report.json",
    "manifest.json",
)

_INSTALL_HINT = "pip install pandas, or pip install 'queuetruth[polars]'"


# ---- backends ---------------------------------------------------------------------------------------------------


def _importable(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def available_backends() -> list[str]:
    """The installed DataFrame libraries among `BACKENDS`, in `BACKENDS` order."""
    return [b for b in BACKENDS if _importable(b)]


def resolve_backend(backend: str | None = None) -> str:
    """The backend to use: `backend`, else env `QT_FRAMES_BACKEND`, else `"auto"` (pandas, then polars).

    An unknown name is `QT-CONFIG-001`; a named or auto backend that is not installed is `QT-USAGE-001`."""
    env = os.environ.get(ENV_BACKEND) if backend is None else None
    source = ENV_BACKEND if env else "backend"
    name = (backend if backend is not None else env or "auto").strip().lower()
    if name not in ("auto", *BACKENDS):
        raise QtError("QT-CONFIG-001", f"{source}: unknown DataFrame backend {name!r} (use auto, pandas or polars)")
    if name == "auto":
        found = available_backends()
        if not found:
            raise QtError(
                "QT-USAGE-001", "queuetruth.frames needs pandas or polars; neither is installed", _INSTALL_HINT
            )
        return found[0]
    if not _importable(name):
        hint = "pip install pandas" if name == "pandas" else "pip install 'queuetruth[polars]'"
        raise QtError("QT-USAGE-001", f"{source}: DataFrame backend {name!r} is not installed", hint)
    return name


def _pandas_types_mapper() -> Any:
    """Arrow type -> pandas dtype: nullable extension dtypes for integers and booleans, `ArrowDtype` for nested
    types (list, large list, fixed-size list, struct, map), None (pyarrow's default) otherwise."""
    pd = importlib.import_module("pandas")
    exact = {
        pa.int8(): pd.Int8Dtype(),
        pa.int16(): pd.Int16Dtype(),
        pa.int32(): pd.Int32Dtype(),
        pa.int64(): pd.Int64Dtype(),
        pa.uint8(): pd.UInt8Dtype(),
        pa.uint16(): pd.UInt16Dtype(),
        pa.uint32(): pd.UInt32Dtype(),
        pa.uint64(): pd.UInt64Dtype(),
        pa.bool_(): pd.BooleanDtype(),
    }
    nested = (
        pa.types.is_list,
        pa.types.is_large_list,
        pa.types.is_fixed_size_list,
        pa.types.is_struct,
        pa.types.is_map,
    )

    def mapper(t: pa.DataType) -> Any:
        found = exact.get(t)
        if found is None and any(f(t) for f in nested):
            return pd.ArrowDtype(t)
        return found

    return mapper


def _schema_name(table: pa.Table) -> str | None:
    meta = table.schema.metadata or {}
    raw = meta.get(b"qt.schema")
    return raw.decode() if raw else None


def _rows_table(rows: Sequence[Mapping[str, Any]], what: str) -> pa.Table:
    """Row dicts as an Arrow table whose columns are the union of every row's keys, in first-seen order (a key a row
    lacks is null there). Values one column cannot hold together (a number and a string, say) are `QT-INPUT-002`."""
    names = list(dict.fromkeys(k for r in rows for k in r))
    try:
        return pa.table({n: [r.get(n) for r in rows] for n in names})
    except (pa.ArrowInvalid, pa.ArrowTypeError) as e:
        raise QtError("QT-INPUT-002", f"{what}: rows with incompatible values in one column: {e}") from None


def _as_table(data: Any) -> pa.Table:
    if isinstance(data, pa.Table):
        return data
    if isinstance(data, pa.RecordBatch):
        return pa.Table.from_batches([data])
    if isinstance(data, Sequence) and not isinstance(data, (str, bytes)):
        if not all(isinstance(r, Mapping) for r in data):
            raise QtError("QT-INPUT-002", "to_frame: a sequence must hold row mappings (dicts)")
        return _rows_table(data, "to_frame")
    raise QtError(
        "QT-INPUT-002",
        f"to_frame: expected a pyarrow.Table, a pyarrow.RecordBatch or a list of row dicts, got {type(data).__name__}",
    )


def to_frame(data: Any, backend: str | None = None) -> Any:
    """One Arrow table (or record batch, or list of row dicts) as a pandas or polars DataFrame (module docstring)."""
    table = _as_table(data)
    name = resolve_backend(backend)
    if name == "pandas":
        df = table.to_pandas(types_mapper=_pandas_types_mapper())
        schema = _schema_name(table)
        if schema:
            df.attrs["qt.schema"] = schema
        return df
    pl = importlib.import_module("polars")
    return pl.from_arrow(table)


def _read_table(path: Path, expect: str | None, columns: Sequence[str] | None = None, filters: Any = None) -> pa.Table:
    """One queuetruth table. Without `columns` and `filters` it is `qt.read_table` (the `qt.schema` metadata, and for
    tables the core knows, every column, are checked). With either, only the metadata and the named columns are
    checked, and pyarrow reads just those columns (and row groups that can match `filters`) from disk."""
    # `queuetruth.api` loads the compiled extension lazily; import it here so `frames` imports without it.
    from queuetruth.api import read_table

    if columns is None and filters is None:
        return read_table(str(path), expect=expect)
    if not path.exists():
        raise QtError("QT-INPUT-001", f"{path}: not found")
    try:
        schema = pq.read_schema(path)
    except (OSError, pa.ArrowInvalid) as e:
        raise QtError("QT-INPUT-002", f"{path}: not a Parquet file: {e}") from None
    name = (schema.metadata or {}).get(b"qt.schema", b"").decode()
    if not name:
        raise QtError("QT-INPUT-002", f"{path}: missing qt.schema metadata (not a queuetruth table)")
    if expect is not None and name != expect:
        raise QtError("QT-INPUT-002", f"{path}: expected table {expect}, found {name}")
    if columns is not None:
        missing = [c for c in columns if c not in schema.names]
        if missing:
            raise QtError("QT-INPUT-002", f"{path}: no column(s) {missing}; columns are {schema.names}")
    try:
        table = pq.read_table(path, columns=None if columns is None else list(columns), filters=filters)
    except (pa.ArrowInvalid, pa.ArrowTypeError, pa.ArrowNotImplementedError, ValueError, TypeError) as e:
        raise QtError("QT-INPUT-002", f"{path}: cannot apply columns/filters: {e}") from None
    if columns is not None:
        table = table.select(list(columns))  # the order asked for
    return table.replace_schema_metadata(schema.metadata)


def read(
    path: str | Path,
    expect: str | None = None,
    columns: Sequence[str] | None = None,
    backend: str | None = None,
    filters: Any = None,
) -> Any:
    """Reads one queuetruth Parquet table as a DataFrame.

    `expect` checks the `qt.schema` metadata (`QT-INPUT-002` on a mismatch). `columns` reads only those columns, in
    that order, and `filters` (pyarrow's DNF form, e.g. `[("instrument_id", "==", 13)]`) only the matching rows;
    both are pushed down to the Parquet reader, so a few columns of a full-day table cost only those columns. An
    unknown column or an unusable filter is `QT-INPUT-002`; a missing file is `QT-INPUT-001`."""
    resolve_backend(backend)
    return to_frame(_read_table(Path(path), expect, columns, filters), backend)


# ---- run directories --------------------------------------------------------------------------------------------


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise QtError("QT-INPUT-001", f"{path}: cannot read: {e}") from None
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise QtError("QT-INPUT-002", f"{path}: not a JSON document: {e}") from None
    schema = data.get("schema") if isinstance(data, dict) else None
    if not isinstance(schema, str) or not schema.startswith("qt."):
        raise QtError("QT-INPUT-002", f"{path}: not a queuetruth document (no qt.* schema field)")
    return data


def _find_summary(run_dir: Path) -> Path:
    for name in SUMMARY_FILES:
        p = run_dir / name
        if p.is_file():
            return p
    raise QtError(
        "QT-INPUT-002",
        f"{run_dir}: no run summary (looked for {', '.join(SUMMARY_FILES)})",
        "pass a run output directory (the `out` of replay, zoo, calibrate, score, ...) or its summary JSON file",
    )


def _listed_files(
    document: Mapping[str, Any], summary_path: Path, base: Path, prefix: str
) -> dict[str, dict[str, Any]]:
    """The `files` entries of one summary by path relative to the run directory `base` (`prefix` is the summary's
    directory relative to `base`). A name that is absolute, contains `..` or resolves outside `base` is
    `QT-INPUT-002`: a summary never names a file outside its run directory."""
    listed = document.get("files")
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(listed, list):
        return out
    root = base.resolve()
    for f in listed:
        if not (isinstance(f, dict) and isinstance(f.get("name"), str)):
            continue
        raw = f["name"]
        rel = PurePosixPath(raw.replace("\\", "/"))
        if not raw or rel.is_absolute() or ".." in rel.parts or PureWindowsPath(raw).drive:
            raise QtError("QT-INPUT-002", f"{summary_path}: file name {raw!r} is not a path inside the run directory")
        name = (PurePosixPath(prefix) / rel).as_posix() if prefix else rel.as_posix()
        try:
            (base / name).resolve().relative_to(root)
        except ValueError:
            raise QtError(
                "QT-INPUT-002", f"{summary_path}: file name {raw!r} resolves outside the run directory {base}"
            ) from None
        out[name] = f
    return out


class RunFrames(Mapping[str, Any]):
    """One run output directory: its summary document and its Parquet tables as DataFrames.

    A read-only mapping from table name (the file's path relative to the run directory without `.parquet`, with `/`
    separators: `zoo_errors`, `held_out/zoo_predictions`) to a DataFrame, read on first access and cached.

    Which tables: when the summary lists its output files (`files`), exactly the listed Parquet files plus those
    listed by nested run summaries (`held_out/summary.json` of a calibration); a stray `.parquet` file nobody lists
    is not a table of the run (`unlisted` names it). When the summary lists no files (`calibration.json`), every
    Parquet file under the directory, since nothing records which belong.

    Attributes: `path` (the run directory), `summary_path`, `document` (the summary as a dict), `schema` (its
    `qt.*` name), `run_id` (or None for documents without one), `backend`, `files` (the `files` entries of the
    summary and of nested run summaries, by path relative to `path`; empty when none lists any), `unlisted`
    (Parquet files under `path` that no summary lists)."""

    def __init__(self, path: Path, summary_path: Path, document: dict[str, Any], backend: str) -> None:
        self.path = path
        self.summary_path = summary_path
        self.document = document
        self.schema: str = document["schema"]
        rid = document.get("run_id")
        self.run_id: str | None = rid if isinstance(rid, str) else None
        self.backend = backend
        own = _listed_files(document, summary_path, path, "")
        self.files: dict[str, dict[str, Any]] = dict(own)
        for nested in sorted(path.rglob("summary.json")):
            if nested == summary_path or nested.parent == path:
                continue
            try:
                doc = json.loads(nested.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue  # not a run summary; nothing to add
            if isinstance(doc, dict) and str(doc.get("schema", "")).startswith("qt."):
                self.files.update(_listed_files(doc, nested, path, nested.parent.relative_to(path).as_posix()))
        missing = sorted(n for n in self.files if not (path / n).is_file())
        if missing:
            raise QtError("QT-INPUT-001", f"{summary_path}: listed output file(s) missing from {path}: {missing}")
        found = {p.relative_to(path).as_posix() for p in path.rglob("*.parquet") if p.is_file()}
        listed = {n for n in self.files if n.endswith(".parquet")}
        self.unlisted: list[str] = sorted(found - listed)
        self._own_listing = bool(own)
        tables = listed if own else listed | found
        self._paths = {n[: -len(".parquet")]: path / n for n in sorted(tables)}
        self._cache: dict[str, Any] = {}

    def __repr__(self) -> str:
        rid = f" run_id={self.run_id}" if self.run_id else ""
        return f"RunFrames({self.schema}{rid}, {len(self)} tables, backend={self.backend}: {self.path})"

    def _path(self, name: str) -> Path:
        if name not in self._paths:
            raise KeyError(f"{name!r}: not a table of this run; tables are {list(self._paths)}")
        return self._paths[name]

    def __getitem__(self, name: str) -> Any:
        if name not in self._cache:
            self._cache[name] = to_frame(self.table(name), self.backend)
        return self._cache[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._paths)

    def __len__(self) -> int:
        return len(self._paths)

    def table(self, name: str) -> pa.Table:
        """The table `name` as a `pyarrow.Table` (not cached; its `qt.schema` metadata is checked)."""
        return _read_table(self._path(name), None)

    def read(self, name: str, columns: Sequence[str] | None = None, filters: Any = None) -> Any:
        """Part of table `name` as a DataFrame, not cached: only `columns` and the rows matching `filters`, pushed
        down to the Parquet reader (as in `queuetruth.frames.read`)."""
        return to_frame(_read_table(self._path(name), None, columns, filters), self.backend)

    def file_path(self, name: str) -> Path:
        """The Parquet file behind table `name`."""
        return self._path(name)

    def model(self) -> QtModel:
        """The summary validated as its Pydantic model (`queuetruth.schemas.parse_document`; `QT-INPUT-002` when it
        does not validate)."""
        from queuetruth.schemas.models import parse_document

        try:
            return parse_document(self.document)
        except ValueError as e:
            raise QtError("QT-INPUT-002", f"{self.summary_path}: {e}") from None

    def verify(self) -> int:
        """Checks every file the summary and nested run summaries list against its recorded `bytes`, `sha256` and
        (for tables) `rows`.

        Returns the number of files checked. Any mismatch is `QT-INPUT-002` naming every bad file; a listed file that
        is missing or unreadable is `QT-INPUT-001`. Emits a `UserWarning` when nothing could be checked (the
        summary lists no files) and when tables of the run are not covered by any listing (a calibration's
        `alloc_events`), or stray Parquet files lie in the directory, since those are not verified."""
        bad: list[str] = []
        for name, entry in sorted(self.files.items()):
            p = self.path / name
            try:
                size = p.stat().st_size
                if isinstance(entry.get("bytes"), int) and size != entry["bytes"]:
                    bad.append(f"{name}: {size} bytes, summary records {entry['bytes']}")
                    continue
                if isinstance(entry.get("sha256"), str) and sha256_file(p) != entry["sha256"]:
                    bad.append(f"{name}: sha256 differs from the summary")
                    continue
                rows = entry.get("rows")
                if isinstance(rows, int) and name.endswith(".parquet"):
                    n = pq.read_metadata(p).num_rows
                    if n != rows:
                        bad.append(f"{name}: {n} rows, summary records {rows}")
            except OSError as e:
                raise QtError("QT-INPUT-001", f"{p}: listed output file cannot be read: {e}") from None
            except pa.ArrowInvalid as e:
                bad.append(f"{name}: not a readable Parquet file ({e})")
        if bad:
            raise QtError(
                "QT-INPUT-002", f"{self.path}: outputs do not match {self.summary_path.name}: " + "; ".join(bad)
            )
        if not self.files:
            warnings.warn(
                f"{self.summary_path}: lists no output files, so nothing was verified", UserWarning, stacklevel=2
            )
        if self.unlisted:
            what = "ignored (not tables of this run)" if self._own_listing else "not verified (no summary lists them)"
            warnings.warn(f"{self.path}: Parquet file(s) {what}: {self.unlisted}", UserWarning, stacklevel=2)
        return len(self.files)


def load_run(path: str | Path, backend: str | None = None, verify: bool = False) -> RunFrames:
    """Opens a run output directory (or its summary JSON file) as a `RunFrames`.

    The summary is the first of `SUMMARY_FILES` present. A missing directory is `QT-INPUT-001`, a directory without
    a summary or a summary that is not a `qt.*` document `QT-INPUT-002`, and a file the summary lists but the
    directory lacks `QT-INPUT-001`, a listed name outside the directory `QT-INPUT-002`. `verify=True` also runs
    `RunFrames.verify()`."""
    p = Path(path)
    name = resolve_backend(backend)
    if p.is_file():
        if p.suffix == ".parquet":
            raise QtError("QT-INPUT-002", f"{p}: a single table, not a run directory", "use queuetruth.frames.read")
        summary_path, run_dir = p, p.parent
    elif p.is_dir():
        run_dir = p
        summary_path = _find_summary(p)
    else:
        raise QtError("QT-INPUT-001", f"{p}: not found")
    run = RunFrames(run_dir, summary_path, _load_json(summary_path), name)
    if verify:
        run.verify()
    return run


# ---- flattened summaries ----------------------------------------------------------------------------------------


def _document(obj: Any, schema: str) -> dict[str, Any]:
    """`obj` (a Pydantic summary, a dict, a `RunFrames`, a run directory or a summary path) as a dict of `schema`."""
    if isinstance(obj, QtModel):
        doc = obj.model_dump(mode="json")
    elif isinstance(obj, RunFrames):
        doc = obj.document
    elif isinstance(obj, Mapping):
        doc = dict(obj)
    elif isinstance(obj, (str, Path)):
        p = Path(obj)
        if p.is_dir():
            p = _find_summary(p)
        elif not p.exists():
            raise QtError("QT-INPUT-001", f"{p}: not found")
        doc = _load_json(p)
    else:
        raise QtError("QT-INPUT-002", f"expected a {schema} document, got {type(obj).__name__}")
    found = doc.get("schema")
    if found != schema:
        raise QtError("QT-INPUT-002", f"expected a {schema} document, got {found!r}")
    return doc


def _f(v: Any) -> float | None:
    return None if v is None else float(v)


def _hkeys(*maps: Mapping[str, Any] | None) -> list[str]:
    keys: set[str] = set()
    for m in maps:
        if m:
            keys.update(m)
    return sorted(keys, key=int)


LEADERBOARD_SCHEMA = pa.schema(
    [
        ("model_id", pa.string()),
        ("horizon_ns", pa.int64()),
        ("fill_rate_bias", pa.float64()),
        ("ci_lo", pa.float64()),
        ("ci_hi", pa.float64()),
        ("exact_fill_rate", pa.float64()),
        ("model_fill_rate", pa.float64()),
        ("brier", pa.float64()),
        ("logloss", pa.float64()),
        ("overfill_bias", pa.float64()),
        ("qty_bias", pa.float64()),
        ("mcnemar_b", pa.int64()),
        ("mcnemar_c", pa.int64()),
        ("mcnemar_p_exact", pa.float64()),
        ("n_leaderboard", pa.int64()),
        ("n_excluded_hbt_312", pa.int64()),
    ]
)


def leaderboard(zoo: Any, backend: str | None = None) -> Any:
    """The zoo leaderboard as one row per model and horizon (`qt.zoo_summary`: a `ZooSummary`, its dict, a zoo
    output directory or its `summary.json`).

    Columns: `model_id`, `horizon_ns`, `fill_rate_bias` with its 95% CI `ci_lo` / `ci_hi`, `exact_fill_rate`,
    `model_fill_rate`, `brier`, `logloss`, then the per-model `overfill_bias`, `qty_bias` and McNemar `b`, `c`,
    `p_exact` repeated on each of the model's rows, and the run's scoring population on every row: `n_leaderboard`
    (never-cancel shadows with an exact label, the same for every model; null when the summary does not report it)
    and `n_excluded_hbt_312` (shadows dropped from every model because a faithful hftbacktest model aborted on them
    at issue #312). Fractions, not percentage points. Models in leaderboard order, horizons ascending; an empty
    leaderboard (a Rust-only run) gives zero rows with these columns."""
    doc = _document(zoo, "qt.zoo_summary")
    n_lb = doc.get("n_leaderboard")
    n_312 = doc.get("n_excluded_hbt_312", 0)
    rows: list[dict[str, Any]] = []
    for e in doc.get("leaderboard") or []:
        frb = e.get("fill_rate_bias") or {}
        brier, logloss = e.get("brier") or {}, e.get("logloss") or {}
        exact, model = e.get("exact_fill_rate") or {}, e.get("model_fill_rate") or {}
        mc = e.get("mcnemar") or {}
        over = e.get("overfill_bias") or {}
        for h in _hkeys(frb, brier, logloss, exact, model):
            ci = frb.get(h) or {}
            rows.append(
                {
                    "model_id": e["model_id"],
                    "horizon_ns": int(h),
                    "fill_rate_bias": _f(ci.get("value")),
                    "ci_lo": _f(ci.get("ci_lo")),
                    "ci_hi": _f(ci.get("ci_hi")),
                    "exact_fill_rate": _f(exact.get(h)),
                    "model_fill_rate": _f(model.get(h)),
                    "brier": _f(brier.get(h)),
                    "logloss": _f(logloss.get(h)),
                    "overfill_bias": _f(over.get("value")),
                    "qty_bias": _f(e.get("qty_bias")),
                    "mcnemar_b": mc.get("b"),
                    "mcnemar_c": mc.get("c"),
                    "mcnemar_p_exact": _f(mc.get("p_exact")),
                    "n_leaderboard": n_lb,
                    "n_excluded_hbt_312": n_312,
                }
            )
    return to_frame(pa.Table.from_pylist(rows, schema=LEADERBOARD_SCHEMA), backend)


HELD_OUT_SCHEMA = pa.schema(
    [
        ("arm", pa.string()),
        ("model_id", pa.string()),
        ("horizon_ns", pa.int64()),
        ("brier", pa.float64()),
        ("logloss", pa.float64()),
        ("fill_rate_bias", pa.float64()),
        ("beats_default", pa.string()),
    ]
)


def held_out(calibration: Any, backend: str | None = None) -> Any:
    """Held-out metrics of a calibration as one row per arm, model and horizon (`qt.calibration_result`: a
    `CalibrationResult`, its dict, a calibration output directory or its `calibration.json`).

    `arm` is `fitted` (`held_out`) or `default` (`baseline_held_out`, the library default on the same test window);
    `beats_default` is the trust assessment's per-horizon comparison (`better`, `tie`, `worse`, `mixed`, `n/a`),
    repeated on both arms, and null when the result carries no trust block. Fitted rows first, models and horizons
    in sorted order."""
    doc = _document(calibration, "qt.calibration_result")
    trust = doc.get("trust") or {}
    beats = trust.get("beats_default") or {}
    rows: list[dict[str, Any]] = []
    for arm, key in (("fitted", "held_out"), ("default", "baseline_held_out")):
        block = doc.get(key) or {}
        for model_id in sorted(block):
            h = block[model_id] or {}
            for hz in _hkeys(h.get("brier"), h.get("logloss"), h.get("fill_rate_bias")):
                rows.append(
                    {
                        "arm": arm,
                        "model_id": model_id,
                        "horizon_ns": int(hz),
                        "brier": _f((h.get("brier") or {}).get(hz)),
                        "logloss": _f((h.get("logloss") or {}).get(hz)),
                        "fill_rate_bias": _f((h.get("fill_rate_bias") or {}).get(hz)),
                        "beats_default": beats.get(hz),
                    }
                )
    return to_frame(pa.Table.from_pylist(rows, schema=HELD_OUT_SCHEMA), backend)


SCORECARDS_SCHEMA = pa.schema(
    [
        ("label", pa.string()),
        ("run_id", pa.string()),
        ("sim_kind", pa.string()),
        ("sim_model_id", pa.string()),
        ("n_orders", pa.int64()),
        ("n_matched", pa.int64()),
        ("both", pa.int64()),
        ("neither", pa.int64()),
        ("live_only", pa.int64()),
        ("sim_only", pa.int64()),
        ("fill_rate_bias", pa.float64()),
        ("ci_lo", pa.float64()),
        ("ci_hi", pa.float64()),
        ("direction", pa.string()),
        ("qty_bias", pa.float64()),
        ("mcnemar_b", pa.int64()),
        ("mcnemar_c", pa.int64()),
        ("mcnemar_p_exact", pa.float64()),
        ("discordance_p", pa.float64()),
        ("gates_passed", pa.bool_()),
        ("headline", pa.string()),
    ]
)


def scorecards(scores: Any, backend: str | None = None) -> Any:
    """Scorecards side by side, one row each (`qt.scorecard_summary`).

    `scores` is one scorecard, a sequence of them, or a mapping label -> scorecard; each may be a
    `ScorecardSummary`, its dict, a score output directory or its `summary.json`. `label` is the mapping key, else
    the simulator's model id, else its kind. Columns: the 2x2 table (`both`, `neither`, `live_only`, `sim_only`),
    `fill_rate_bias` (sim minus live) with its CI and `direction`, `qty_bias`, the McNemar counts and exact p,
    `discordance_p` (the cluster-robust primary test; null when the scorecard does not report it), `gates_passed`
    and the verdict `headline`."""
    if isinstance(scores, Mapping) and not isinstance(scores, RunFrames) and "schema" not in scores:
        items: list[tuple[str | None, Any]] = [(str(k), v) for k, v in scores.items()]
    elif isinstance(scores, (QtModel, Mapping, str, Path, RunFrames)):
        items = [(None, scores)]
    elif isinstance(scores, Iterable):
        items = [(None, s) for s in scores]
    else:
        raise QtError("QT-INPUT-002", f"scorecards: expected scorecards, got {type(scores).__name__}")
    rows: list[dict[str, Any]] = []
    for label, s in items:
        doc = _document(s, "qt.scorecard_summary")
        src = doc.get("sim_source") or {}
        t = doc.get("two_by_two") or {}
        b = doc.get("bias") or {}
        mc = doc.get("mcnemar") or {}
        dc = doc.get("discordance_clustered") or {}
        v = doc.get("verdict") or {}
        rows.append(
            {
                "label": label or src.get("model_id") or src.get("kind"),
                "run_id": doc.get("run_id"),
                "sim_kind": src.get("kind"),
                "sim_model_id": src.get("model_id"),
                "n_orders": doc.get("n_orders"),
                "n_matched": doc.get("n_matched"),
                "both": t.get("both"),
                "neither": t.get("neither"),
                "live_only": t.get("live_only"),
                "sim_only": t.get("sim_only"),
                "fill_rate_bias": _f(b.get("fill_rate_bias")),
                "ci_lo": _f(b.get("ci_lo")),
                "ci_hi": _f(b.get("ci_hi")),
                "direction": b.get("direction"),
                "qty_bias": _f(b.get("qty_bias")),
                "mcnemar_b": mc.get("b"),
                "mcnemar_c": mc.get("c"),
                "mcnemar_p_exact": _f(mc.get("p_exact")),
                "discordance_p": _f(dc.get("p_value")),
                "gates_passed": v.get("gates_passed"),
                "headline": v.get("headline"),
            }
        )
    return to_frame(pa.Table.from_pylist(rows, schema=SCORECARDS_SCHEMA), backend)


def _flatten_rows(items: Sequence[Mapping[str, Any]], what: str) -> list[dict[str, Any]]:
    """Nested objects flattened to dotted keys, every row with the same columns in first-seen order over all rows.

    A key that holds an object in one row and null (or nothing) in another gives null in each of the object's columns
    there, so a later row's nested values are never lost. A key that holds an object in one row and a non-null scalar
    or list in another, or two paths that flatten to the same dotted name (`{"a.b": 1}` and `{"a": {"b": 2}}`), are
    `QT-INPUT-002`."""
    objects: set[tuple[str, ...]] = set()
    scalars: set[tuple[str, ...]] = set()
    leaves: dict[tuple[str, ...], None] = {}

    def walk(row: Mapping[str, Any], prefix: tuple[str, ...]) -> None:
        for k, v in row.items():
            path = (*prefix, str(k))
            if isinstance(v, Mapping):
                objects.add(path)
                walk(v, path)
            else:
                if v is not None:
                    scalars.add(path)
                leaves.setdefault(path, None)

    for r in items:
        walk(r, ())
    clash = sorted(objects & scalars)
    if clash:
        raise QtError("QT-INPUT-002", f"{what}: {'.'.join(clash[0])!r} is an object in one row and a value in another")
    columns = [p for p in leaves if p not in objects]
    names: dict[str, tuple[str, ...]] = {}
    for path in columns:
        name = ".".join(path)
        if name in names:
            raise QtError("QT-INPUT-002", f"{what}: two fields flatten to the same column {name!r}")
        names[name] = path

    def get(row: Mapping[str, Any], path: tuple[str, ...]) -> Any:
        v: Any = row
        for k in path:
            if not isinstance(v, Mapping):
                return None
            v = v.get(k)
        return v

    return [{name: get(r, path) for name, path in names.items()} for r in items]


def records(doc: Any, field: str, backend: str | None = None) -> Any:
    """A list-of-objects field of any summary document as a DataFrame, one row per element.

    `doc` is a Pydantic summary, a dict, a `RunFrames` or a run directory / summary path; `field` is a top-level
    field holding a list of objects (`markouts`, `by_queue_decile`, `stratified`, ...). Nested objects are
    flattened to dotted column names (`fill_rate_bias.value`); the columns are the union over all elements in
    first-seen order, null where an element lacks the field. Column types are inferred from the values. An absent or
    null field, or values one column cannot hold together, is `QT-INPUT-002`; an empty list gives an empty frame."""
    if isinstance(doc, QtModel):
        data = doc.model_dump(mode="json")
    elif isinstance(doc, RunFrames):
        data = doc.document
    elif isinstance(doc, Mapping):
        data = dict(doc)
    elif isinstance(doc, (str, Path)):
        p = Path(doc)
        data = _load_json(_find_summary(p) if p.is_dir() else p) if p.exists() else None
        if data is None:
            raise QtError("QT-INPUT-001", f"{p}: not found")
    else:
        raise QtError("QT-INPUT-002", f"records: expected a summary document, got {type(doc).__name__}")
    value = data.get(field)
    if not isinstance(value, list):
        kind = "absent" if field not in data else type(value).__name__
        raise QtError("QT-INPUT-002", f"records: field {field!r} of {data.get('schema')} is not a list ({kind})")
    if not all(isinstance(r, Mapping) for r in value):
        raise QtError("QT-INPUT-002", f"records: field {field!r} holds non-object items")
    what = f"records: field {field!r}"
    return to_frame(_rows_table(_flatten_rows(value, what), what), backend)
