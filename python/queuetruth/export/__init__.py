"""hftbacktest / NautilusTrader config text and the IntpOrderLatency npz (SPEC 4.3 `queuetruth.export`, 5.10).

`hftbacktest_snippet` and `nautilus_snippet` render the SPEC 5.10 layouts from a `qt.calibration_result`
(including one produced by `queuetruth.score.calibrate_live`), with header lines stating the fit, its events and the
held-out result; `export_snippets` fills the result's `exports` object; `run_export` implements the Python export
targets (`hftbacktest`, `nautilus`, `latency_npz`) and emits the snippet's notes as `ExportNote` warnings (the CLI
prints them on stderr).

Fail-safe (trader review D1, D2, D4): `run_export` refuses a calibration that is degenerate, at a grid edge or does
not beat the default out of sample (`queuetruth.export.trust`, `ExportRefused` = `QT-CALIB-001`, exit code 1)
unless `force=True`; the forced snippet carries a `# !! NOT VALIDATED` header with the reasons.

Schedule replay (trader review F11): `hftbacktest_replay_script` and `nautilus_replay_strategy` turn a canonical
live order schedule (`qt.shadow_spec` legs + `qt.shadow_amend` rows) into a replay program for the user's own
simulator whose output `queuetruth.live.import_sim` reads (`queuetruth.export.replay`).
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any

from queuetruth.errors import QtError
from queuetruth.export.hftbacktest import hftbacktest_notes, hftbacktest_snippet
from queuetruth.export.latency import (
    LATENCY_DTYPE,
    LatencyRowsSkipped,
    latency_rows,
    latency_table,
    write_latency_npz,
    write_npz,
)
from queuetruth.export.nautilus import nautilus_notes, nautilus_snippet
from queuetruth.export.replay import (
    ReplayExport,
    hftbacktest_replay_script,
    nautilus_replay_strategy,
    read_schedule,
    replay_schedule,
)
from queuetruth.export.trust import ExportRefused, annotate, assess, refusal
from queuetruth.schemas.models import CalibrationResult, Exports, ExportSnippet, ExportSummary
from queuetruth.schemas.specs import ExportSpec


class ExportNote(UserWarning):
    """A caveat that accompanies an exported snippet (hftbacktest #312/#316, conflated L2, omitted lines). Emitted by
    `run_export` so `queuetruth export hftbacktest|nautilus` prints it on stderr (`warning: ...`) next to the text,
    which keeps the exact SPEC 5.10 layout."""


def export_snippets(cal: CalibrationResult, latency_npz: str | None = None) -> Exports:
    """The `exports` object of a calibration result (snippets + notes; `latency_npz` kept or set)."""
    npz = latency_npz if latency_npz is not None else cal.exports.latency_npz
    c = cal.model_copy(update={"exports": Exports(latency_npz=npz)})
    return Exports(
        hftbacktest=ExportSnippet(python=hftbacktest_snippet(c), notes=hftbacktest_notes(c)),
        nautilus=ExportSnippet(python=nautilus_snippet(c), notes=nautilus_notes(c)),
        latency_npz=npz,
    )


def load_calibration(path: str | Path) -> CalibrationResult:
    """Reads a `qt.calibration_result` JSON file (`calibration.json` or `fit.json`, or a directory holding one)."""
    p = Path(path)
    if p.is_dir():
        for name in ("calibration.json", "fit.json"):
            if (p / name).exists():
                p = p / name
                break
    if not p.exists():
        raise QtError("QT-INPUT-001", f"{p}: calibration result not found")
    try:
        doc: Any = json.loads(p.read_text(encoding="utf-8"))
        return CalibrationResult.model_validate(doc)
    except (json.JSONDecodeError, ValueError) as e:
        raise QtError("QT-INPUT-002", f"{p}: not a qt.calibration_result: {e}") from e


def run_export(spec: ExportSpec | dict[str, Any], *, force: bool = False) -> ExportSummary:
    """Python export targets: `hftbacktest`/`nautilus` (text, optionally written to `out`) and `latency_npz`.

    `hftbacktest`/`nautilus` raise `ExportRefused` (`QT-CALIB-001`, exit code 1) for a calibration that
    `queuetruth.export.trust.assess` refuses, unless `force` or `spec.force` (schema 1.1, CLI `--force`); nothing is
    written then."""
    s = ExportSpec.model_validate(spec) if isinstance(spec, dict) else spec
    files: list[dict[str, Any]] = []
    if s.target in ("hftbacktest", "nautilus"):
        ref = s.calibration
        if ref is None or not hasattr(ref, "path"):
            raise QtError("QT-CONFIG-001", "export: calibration (local path) is required", "--calibration FILE")
        cal = load_calibration(ref.path)  # type: ignore[union-attr]
        force = force or s.force
        refused = refusal(cal, s.target)
        if refused is not None and not force:
            raise refused
        if refused is not None:
            warnings.warn(ExportNote(f"--force: writing a refused calibration ({refused.message})"), stacklevel=2)
        if s.target == "hftbacktest":
            text, notes = hftbacktest_snippet(cal), hftbacktest_notes(cal)
        else:
            text, notes = nautilus_snippet(cal), nautilus_notes(cal)
        for note in notes:
            warnings.warn(ExportNote(note), stacklevel=2)
        if s.out:
            p = Path(s.out)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
            from queuetruth.score._io import file_entry

            files.append(file_entry(p))
        return ExportSummary(target=s.target, files=files, text=text)
    if s.target == "latency_npz":
        if s.live is None or not hasattr(s.live, "path"):
            raise QtError("QT-CONFIG-001", "export latency-npz: live (local path) is required", "--live DIR")
        if not s.out:
            raise QtError("QT-CONFIG-001", "export latency-npz: out is required", "--out FILE")
        entry = write_latency_npz(s.live.path, s.out)  # type: ignore[union-attr]
        return ExportSummary(target=s.target, files=[entry], text=None)
    raise QtError("QT-CONFIG-001", f"export target {s.target} is native (queuetruth._core.export_l2_npz)")


__all__ = [
    "LATENCY_DTYPE",
    "ExportNote",
    "ExportRefused",
    "LatencyRowsSkipped",
    "ReplayExport",
    "annotate",
    "assess",
    "export_snippets",
    "hftbacktest_notes",
    "hftbacktest_replay_script",
    "hftbacktest_snippet",
    "latency_rows",
    "latency_table",
    "load_calibration",
    "nautilus_notes",
    "nautilus_replay_strategy",
    "nautilus_snippet",
    "read_schedule",
    "refusal",
    "replay_schedule",
    "run_export",
    "write_latency_npz",
    "write_npz",
]
