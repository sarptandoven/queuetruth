"""Run directories: which command wrote them, which inputs their spec names, run identity and re-execution.

A run directory is the `--out` of one of the commands below; its summary document carries the spec echo (SPEC 7.3:
execution-only fields removed, paths as given) from which the run can be executed again:

| kind | document | schema | Python API |
|---|---|---|---|
| `calibrate`, `calibrate_live` | `calibration.json` | `qt.calibration_result` | `calibrate`, `calibrate_live` |
| `fit_hazard` | `model.json` | `qt.hazard_model_card` | `fit_hazard` |
| `referee` | `referee_report.json` | `qt.referee_report` | `referee` |
| `dq` | `dq_report.json` | `qt.dq_report` | `dq` |
| `replay` | `summary.json` | `qt.replay_summary` | `replay` |
| `zoo` | `summary.json` | `qt.zoo_summary` | `run_zoo` |
| `score` | `summary.json` | `qt.scorecard_summary` | `score` |

`ingest`, `shadows sample`, `features` and `report` write no summary document into their output (it goes to stdout
only), so they are certified as inputs of the runs that use them, or as `--attach` attachments.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from queuetruth.cert.hashing import escape_token, map_path
from queuetruth.errors import QtError
from queuetruth.schemas.runid import is_ref, run_id

#: (document file, schema, kind) in detection order (the first match wins; `cal/held_out/summary.json` belongs to
#: the calibration run, so `calibration.json` comes before `summary.json`).
DOCUMENTS: tuple[tuple[str, str, str], ...] = (
    ("calibration.json", "qt.calibration_result", "calibrate"),
    ("model.json", "qt.hazard_model_card", "fit_hazard"),
    ("referee_report.json", "qt.referee_report", "referee"),
    ("dq_report.json", "qt.dq_report", "dq"),
    ("summary.json", "qt.replay_summary", "replay"),
    ("summary.json", "qt.zoo_summary", "zoo"),
    ("summary.json", "qt.scorecard_summary", "score"),
)
#: kind -> `queuetruth.api` function that re-executes it.
API_FUNCTIONS = {
    "replay": "replay",
    "zoo": "run_zoo",
    "calibrate": "calibrate",
    "calibrate_live": "calibrate_live",
    "fit_hazard": "fit_hazard",
    "score": "score",
    "referee": "referee",
    "dq": "dq",
}


@dataclass
class RunDoc:
    """A detected run: its kind, summary document and recorded identity."""

    kind: str
    document: str
    schema: str
    doc: dict[str, Any]
    spec: dict[str, Any]
    #: The run id the document records (`run_id`, or the suffix of a hazard `model_id`); None when it records none.
    recorded_run_id: str | None
    engine_version: str | None


def _load_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except OSError as e:
        raise QtError("QT-INPUT-001", f"{p}: {e.strerror or e}") from None
    except (UnicodeDecodeError, ValueError) as e:
        raise QtError("QT-INPUT-002", f"{p}: invalid JSON ({e})") from None


def detect(run_dir: Path) -> RunDoc:
    """The run in `run_dir`; `QT-INPUT-002` when no supported summary document is there."""
    if not run_dir.is_dir():
        raise QtError("QT-INPUT-001", f"{run_dir}: run directory not found")
    seen: list[str] = []
    for name, schema, kind in DOCUMENTS:
        p = run_dir / name
        if not p.is_file():
            continue
        doc = _load_json(p)
        got = doc.get("schema") if isinstance(doc, dict) else None
        seen.append(f"{name}: {got}")
        if got != schema:
            continue
        spec = doc.get("spec")
        if not isinstance(spec, dict):
            raise QtError("QT-INPUT-002", f"{p}: no spec echo; the run cannot be certified")
        if kind == "calibrate" and doc.get("kind") == "live":
            kind = "calibrate_live"
        recorded = doc.get("run_id") if isinstance(doc.get("run_id"), str) else None
        if kind == "fit_hazard" and isinstance(doc.get("model_id"), str) and ":" in doc["model_id"]:
            recorded = doc["model_id"].rsplit(":", 1)[1]
        ev = doc.get("engine_version")
        return RunDoc(kind, name, schema, doc, spec, recorded, ev if isinstance(ev, str) else None)
    found = f" (found {', '.join(seen)})" if seen else ""
    raise QtError(
        "QT-INPUT-002",
        f"{run_dir}: not a queuetruth run directory{found}",
        "certify the --out directory of replay, zoo, calibrate, calibrate-live, fit-hazard, score, referee run or "
        "dq; datasets, shadow files and live logs are certified as the inputs of those runs (or with --attach)",
    )


#: Top-level keys a command adds to its spec echo after computing `run_id` (annotations, not inputs of the run):
#: `calibrate-live` marks its result `calibration_source: "live"` for `export`.
ECHO_ANNOTATIONS: dict[str, tuple[str, ...]] = {"calibrate_live": ("calibration_source",)}


def executable_spec(kind: str, spec: dict[str, Any]) -> dict[str, Any]:
    """The spec a run was executed with (and its `run_id` computed from): the echo without the annotations of
    `ECHO_ANNOTATIONS`."""
    drop = ECHO_ANNOTATIONS.get(kind, ())
    return {k: v for k, v in spec.items() if k not in drop} if drop else spec


def spec_refs(spec: Any, pointer: str = "") -> list[tuple[str, dict[str, Any]]]:
    """(JSON pointer, ref object) of every `DatasetRef` / `FileRef` in a spec echo, in document order."""
    out: list[tuple[str, dict[str, Any]]] = []
    if is_ref(spec):
        return [(pointer, spec)]
    if isinstance(spec, dict):
        for k, v in spec.items():
            out.extend(spec_refs(v, f"{pointer}/{escape_token(k)}"))
    elif isinstance(spec, list):
        for i, v in enumerate(spec):
            out.extend(spec_refs(v, f"{pointer}/{i}"))
    return out


def map_spec(spec: Any, mapping: list[tuple[str, str]]) -> Any:
    """A copy of `spec` with every `{"path": ...}` reference remapped."""
    if not mapping:
        return spec
    if is_ref(spec) and isinstance(spec.get("path"), str):
        return {"path": map_path(spec["path"], mapping)}
    if isinstance(spec, dict):
        return {k: map_spec(v, mapping) for k, v in spec.items()}
    if isinstance(spec, list):
        return [map_spec(v, mapping) for v in spec]
    return spec


def resolve_under(base: Path, path: str) -> Path:
    """A spec path resolved the way the command resolved it: relative paths against `base` (the directory the
    command ran in)."""
    p = Path(path)
    return p if p.is_absolute() else base / p


def recompute_run_id(spec: dict[str, Any], engine_version: str, identity: Callable[[dict[str, Any]], str]) -> str:
    """SPEC 7.3 `run_id` of a spec echo with references resolved by `identity`."""
    return run_id(spec, engine_version, identity)


def python_call(kind: str, spec: dict[str, Any], out: str) -> str:
    """A one-line Python reproduction of a run (`out` set to a fresh directory)."""
    fn = API_FUNCTIONS[kind]
    body = json.dumps({**executable_spec(kind, spec), "out": out}, ensure_ascii=False, sort_keys=False)
    return f"import json, queuetruth.api as a; a.{fn}(json.loads({body!r}))"
