"""Discovery of report inputs: queuetruth output directories scanned for JSON documents (by their `schema` field)
and Parquet tables (by their `qt.schema` metadata), with sha256 provenance for every file the report reads.

A directory whose top level holds no queuetruth artifact is expanded to its immediate subdirectories (sorted), so a
parent folder of several runs can be passed as one input. Paths are shown as given (never made absolute).
Dataset and live-log manifests referenced by a spec echo (`dataset.path`, `live.path`) are read when they resolve
from the current directory; they feed the provenance banner only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from queuetruth.errors import QtError
from queuetruth.schemas.arrow import SCHEMAS
from queuetruth.schemas.io import sha256_file
from queuetruth.schemas.models import (
    DOC_MODELS,
    CalibrationResult,
    DataQuality,
    DatasetManifest,
    IngestSummary,
    LiveImportSummary,
    QtDoc,
    ReplaySummary,
    ScorecardSummary,
)


@dataclass
class FileRec:
    """One file the report read: display path, kind (`qt.*` schema name), rows (tables), bytes, sha256."""

    path: str
    schema: str
    bytes: int
    sha256: str
    rows: int | None = None
    role: str = "input"  # input | provenance


@dataclass
class Doc:
    """A validated JSON document."""

    rec: FileRec
    name: str
    model: QtDoc


@dataclass
class Extra:
    """A JSON document whose schema is known to the report but not registered in `DOC_MODELS`."""

    rec: FileRec
    name: str
    data: dict[str, Any]


#: Unregistered JSON documents the report reads, with the keys each must carry.
EXTRA_DOCS: dict[str, tuple[str, ...]] = {
    "qt.live_calibration_test": ("model_id", "n", "b", "c"),
}


@dataclass
class Table:
    """A Parquet table with `qt.schema` metadata; read lazily."""

    rec: FileRec
    name: str
    abspath: Path
    _cache: pa.Table | None = None

    def read(self) -> pa.Table:
        """The whole table; required columns of the frozen schema are checked (`QT-INPUT-002`)."""
        if self._cache is None:
            try:
                t = pq.read_table(self.abspath)
            except (OSError, pa.ArrowException) as e:
                raise QtError("QT-INPUT-002", f"{self.rec.path}: unreadable Parquet: {e}") from e
            want = SCHEMAS.get(self.rec.schema)
            if want is not None:
                missing = [f.name for f in want if f.name not in t.column_names]
                if missing:
                    raise QtError("QT-INPUT-002", f"{self.rec.path}: {self.rec.schema} lacks columns {missing}")
            self._cache = t
        return self._cache


@dataclass
class RunDir:
    """The artifacts of one output directory."""

    label: str
    root: Path
    docs: list[Doc] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    superseded: list[Doc] = field(default_factory=list)
    extras: list[Extra] = field(default_factory=list)

    def extra(self, schema: str) -> dict[str, Any] | None:
        """First raw JSON document of a schema not (yet) registered in SPEC 3.10, e.g. `qt.live_calibration_test`."""
        for e in self.extras:
            if e.rec.schema == schema:
                return e.data
        return None

    def docs_of(self, cls: type[QtDoc]) -> list[Any]:
        """Documents of one model class, in file-name order."""
        return [d.model for d in self.docs if isinstance(d.model, cls)]

    def table(self, schema: str) -> Table | None:
        """First table with the given `qt.schema` (file-name order)."""
        for t in self.tables:
            if t.rec.schema == schema:
                return t
        return None


@dataclass
class Provenance:
    """Validation statuses found for the data behind the report."""

    entries: list[tuple[str, str]] = field(default_factory=list)  # (source description, status)
    price_exp: dict[int, int | None] = field(default_factory=dict)

    def add(self, what: str, status: str) -> None:
        if (what, status) not in self.entries:
            self.entries.append((what, status))

    def level(self) -> str:
        """`synthetic`, `assumed`, `real` or `unknown`."""
        st = {s for _, s in self.entries}
        if not st:
            return "unknown"
        if st & {"synthetic_only", "fixture_only"}:
            return "synthetic"
        if "assumed" in st:
            return "assumed"
        return "real"


@dataclass
class Inputs:
    """Everything the report reads."""

    given: list[str]
    runs: list[RunDir]
    files: list[FileRec]
    provenance: Provenance
    integrity: list[str]  # files whose sha256 differs from the summary that listed them


def _display(base: str, *parts: str) -> str:
    return "/".join([base.rstrip("/"), *parts]) if base not in ("", ".") else "/".join(parts)


def _load_json(p: Path) -> Any:
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _scan(root: Path, label: str, only: str | None = None) -> RunDir:
    run = RunDir(label=label, root=root)
    for p in sorted(root.iterdir(), key=lambda q: q.name):
        if not p.is_file() or (only is not None and p.name != only):
            continue
        if p.suffix == ".json":
            data = _load_json(p)
            if not isinstance(data, dict):
                continue
            name = data.get("schema")
            if isinstance(name, str) and name in EXTRA_DOCS:
                missing = [k for k in EXTRA_DOCS[name] if k not in data]
                if missing:
                    raise QtError("QT-INPUT-002", f"{_display(label, p.name)}: {name} lacks {missing}")
                st = p.stat()
                rec = FileRec(_display(label, p.name), name, st.st_size, sha256_file(p))
                run.extras.append(Extra(rec, p.name, data))
                continue
            if not isinstance(name, str) or name not in DOC_MODELS:
                continue
            try:
                model = DOC_MODELS[name].model_validate(data)
            except ValueError as e:
                raise QtError("QT-INPUT-002", f"{_display(label, p.name)}: invalid {name} document: {e}") from e
            st = p.stat()
            rec = FileRec(_display(label, p.name), name, st.st_size, sha256_file(p))
            run.docs.append(Doc(rec, p.name, model))
        elif p.suffix == ".parquet":
            try:
                sch = pq.read_schema(p)
                rows = pq.ParquetFile(p).metadata.num_rows
            except (OSError, pa.ArrowException) as e:
                raise QtError("QT-INPUT-002", f"{_display(label, p.name)}: unreadable Parquet: {e}") from e
            name_b = (sch.metadata or {}).get(b"qt.schema")
            if name_b is None:
                continue
            name = name_b.decode("utf-8", "replace")
            st = p.stat()
            rec = FileRec(_display(label, p.name), name, st.st_size, sha256_file(p), rows=rows)
            run.tables.append(Table(rec, p.name, p))
    _dedupe(run)
    return run


def _richness(c: CalibrationResult) -> int:
    e = c.exports
    return int(bool(c.held_out)) + int(bool(e.hftbacktest or e.nautilus or e.latency_npz))


def _dedupe(run: RunDir) -> None:
    """Keeps the richest `qt.calibration_result` per run_id (calibration.json over the Rust-only fit.json) and drops
    standalone `qt.data_quality` documents already carried by a replay or scorecard summary of the same directory."""
    cals = [d for d in run.docs if isinstance(d.model, CalibrationResult)]
    drop: set[int] = set()
    by_run: dict[tuple[str, str], list[Doc]] = {}
    for d in cals:
        m = d.model
        assert isinstance(m, CalibrationResult)
        by_run.setdefault((m.run_id, m.family), []).append(d)
    for group in by_run.values():
        if len(group) > 1:
            keep = max(group, key=lambda d: (_richness(d.model), d.name == "calibration.json"))  # type: ignore[arg-type]
            drop.update(id(d) for d in group if d is not keep)
    carried = any(
        isinstance(d.model, ReplaySummary | ScorecardSummary) and getattr(d.model, "data_quality", None) is not None
        for d in run.docs
    )
    if carried:
        drop.update(id(d) for d in run.docs if isinstance(d.model, DataQuality))
    run.superseded = [d for d in run.docs if id(d) in drop]
    for d in run.superseded:
        d.rec.role = "superseded"
    run.docs = [d for d in run.docs if id(d) not in drop]


def _has_artifacts(run: RunDir) -> bool:
    return bool(run.docs or run.tables or run.extras)


def _spec_path(spec: dict[str, Any], key: str) -> str | None:
    v = spec.get(key)
    if isinstance(v, dict) and isinstance(v.get("path"), str):
        return v["path"]
    return None


def _provenance(runs: list[RunDir], files: list[FileRec]) -> Provenance:
    prov = Provenance()
    seen_paths: set[str] = set()
    conflicts: set[int] = set()

    def note_manifest(m: DatasetManifest, where: str) -> None:
        prov.add(f"dataset {m.dataset_id[:12]} ({m.source_format}, venue {m.venue}) from {where}", m.validation_status)
        for k, v in m.price_exp_by_instrument.items():
            iid = int(k)
            if iid in prov.price_exp and prov.price_exp[iid] != v:
                conflicts.add(iid)
            prov.price_exp[iid] = v

    for run in runs:
        for d in run.docs:
            m = d.model
            if isinstance(m, DatasetManifest):
                note_manifest(m, d.rec.path)
            elif isinstance(m, IngestSummary):
                note_manifest(m.manifest, d.rec.path)
            elif isinstance(m, CalibrationResult):
                for s in m.data_sources:
                    prov.add(
                        f"dataset {s.dataset_id[:12]} (venue {s.venue}/{s.source_venue}) per {d.rec.path}",
                        s.validation_status,
                    )
            elif isinstance(m, LiveImportSummary):
                prov.add(
                    f"live log ({m.venue}) per {d.rec.path}",
                    "real_data" if m.validation_status == "real" else "fixture_only",
                )
        for d in run.docs:
            spec = getattr(d.model, "spec", None)
            if not isinstance(spec, dict):
                continue
            ds = _spec_path(spec, "dataset")
            if ds and ds not in seen_paths:
                seen_paths.add(ds)
                mp = Path(ds) / "manifest.json"
                data = _load_json(mp) if mp.is_file() else None
                if isinstance(data, dict) and data.get("schema") == "qt.dataset_manifest":
                    try:
                        man = DatasetManifest.model_validate(data)
                    except ValueError:
                        man = None
                    if man is not None:
                        files.append(
                            FileRec(
                                _display(ds, "manifest.json"),
                                "qt.dataset_manifest",
                                mp.stat().st_size,
                                sha256_file(mp),
                                role="provenance",
                            )
                        )
                        note_manifest(man, _display(ds, "manifest.json"))
            lv = _spec_path(spec, "live")
            if lv and lv not in seen_paths:
                seen_paths.add(lv)
                op = Path(lv) / "orders.parquet"
                if op.is_file():
                    try:
                        col = pq.read_table(op, columns=["validation_status"]).column(0).to_pylist()
                    except (OSError, pa.ArrowException, KeyError):
                        col = []
                    if col:
                        files.append(
                            FileRec(
                                _display(lv, "orders.parquet"),
                                "qt.live_orders",
                                op.stat().st_size,
                                sha256_file(op),
                                rows=len(col),
                                role="provenance",
                            )
                        )
                    for v in sorted({str(x) for x in col if x is not None}):
                        prov.add(f"live log {lv}", "real_data" if v == "real" else "fixture_only")
    for iid in conflicts:
        prov.price_exp[iid] = None
    return prov


def _integrity(runs: list[RunDir]) -> list[str]:
    """Files listed in a summary's `files` whose current sha256 differs."""
    out: list[str] = []
    for run in runs:
        actual = {Path(r.rec.path).name: r.rec.sha256 for r in [*run.docs, *run.superseded, *run.extras, *run.tables]}
        for d in run.docs:
            for f in getattr(d.model, "files", None) or []:
                name = Path(f.name).name
                if name in actual and actual[name] != f.sha256:
                    out.append(f"{_display(run.label, name)} (listed by {d.rec.path})")
    return sorted(set(out))


def load_inputs(given: list[str]) -> Inputs:
    """Scans every input (directory or single JSON/Parquet file); `QT-INPUT-001` when an input does not exist,
    `QT-INPUT-002` when nothing recognizable is found."""
    runs: list[RunDir] = []
    seen: list[str] = []
    for g in given:
        if g in seen:
            continue
        seen.append(g)
        p = Path(g)
        if p.is_file():
            label = str(p.parent) if p.parent != Path(".") else "."
            parent = _scan(p.parent, label, only=p.name)
            if _has_artifacts(parent):
                runs.append(parent)
            continue
        if not p.is_dir():
            raise QtError(
                "QT-INPUT-001",
                f"{g}: report input not found",
                hint="pass output directories of score, zoo, calibrate, fit-hazard or replay",
            )
        top = _scan(p, g)
        if _has_artifacts(top):
            runs.append(top)
            continue
        for sub in sorted((q for q in p.iterdir() if q.is_dir()), key=lambda q: q.name):
            r = _scan(sub, _display(g, sub.name))
            if _has_artifacts(r):
                runs.append(r)
    if not runs:
        raise QtError(
            "QT-INPUT-002",
            "no queuetruth outputs found in the report inputs",
            hint="inputs must contain summary JSON documents or qt.* Parquet tables",
        )
    files = [r.rec for run in runs for r in [*run.docs, *run.superseded, *run.extras, *run.tables]]
    prov = _provenance(runs, files)
    return Inputs(given=seen, runs=runs, files=files, provenance=prov, integrity=_integrity(runs))
