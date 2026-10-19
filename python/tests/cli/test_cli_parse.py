"""`_core.cli_parse` from Python: the golden parsed commands of `crates/cli/tests/golden/cli_parse.json` (every
command and flag), the arguments of every spec command validate against its Pydantic spec model, and `CliExit`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from queuetruth.schemas import specs as s
from queuetruth.score import LiveCalibrationSpec

pytestmark = pytest.mark.requires_core

SPEC_OF: dict[tuple[str, ...], type[BaseModel]] = {
    ("ingest",): s.IngestSpec,
    ("replay",): s.ReplaySpec,
    ("shadows", "sample"): s.ShadowSampleSpec,
    ("shadows", "mirror-real"): s.MirrorSpec,
    ("zoo",): s.ZooSpec,
    ("calibrate", "alloc"): s.CalibrationSpec,
    ("calibrate", "survival"): s.CalibrationSpec,
    ("calibrate", "nt-touch"): s.CalibrationSpec,
    ("calibrate", "latency"): s.CalibrationSpec,
    ("calibrate-live",): LiveCalibrationSpec,
    ("features",): s.FeatureSpec,
    ("fit-hazard",): s.HazardFitSpec,
    ("live", "import"): s.LiveImportSpec,
    ("score",): s.ScorecardSpec,
    ("report",): s.ReportSpec,
    ("referee", "run"): s.RefereeSpec,
    ("why",): s.WhySpec,
    ("export", "hftbacktest"): s.ExportSpec,
    ("export", "nautilus"): s.ExportSpec,
    ("export", "l2-npz"): s.ExportSpec,
    ("export", "latency-npz"): s.ExportSpec,
}


def _golden(repo_root: Path) -> list[dict[str, Any]]:
    return json.loads((repo_root / "crates" / "cli" / "tests" / "golden" / "cli_parse.json").read_text())


def _assert_subset(given: Any, dumped: Any, where: Any) -> None:
    if isinstance(given, dict):
        assert isinstance(dumped, dict), where
        for k, v in given.items():
            assert k in dumped, (where, k)
            _assert_subset(v, dumped[k], (where, k))
    elif isinstance(given, list):
        assert isinstance(dumped, list) and len(dumped) == len(given), where
        for i, (g, d) in enumerate(zip(given, dumped, strict=True)):
            _assert_subset(g, d, (where, i))
    else:
        assert given == dumped, where


def test_python_parse_matches_golden(repo_root: Path) -> None:
    from queuetruth import _core

    cases = _golden(repo_root)
    assert len(cases) >= 40
    for case in cases:
        assert json.loads(_core.cli_parse(case["argv"])) == case["command"], case["argv"]


def test_parsed_args_validate_against_specs(repo_root: Path) -> None:
    seen = set()
    for case in _golden(repo_root):
        cmd = case["command"]
        key = tuple(cmd["path"])
        model = SPEC_OF.get(key)
        if model is None:
            continue
        seen.add(key)
        args = {k: v for k, v in cmd["args"].items() if k != "_cli"}
        spec = model.model_validate(args)
        # Every given field survives validation unchanged (no silent renames or drops).
        _assert_subset(args, spec.model_dump(mode="json", by_alias=True), key)
    assert seen == set(SPEC_OF)


def test_cli_exit_attributes() -> None:
    from queuetruth import _core

    with pytest.raises(_core.CliExit) as e:
        _core.cli_parse(["queuetruth", "--help"])
    assert (e.value.code, e.value.to_stderr) == (0, False) and "Usage: queuetruth" in e.value.text
    with pytest.raises(_core.CliExit) as e:
        _core.cli_parse(["queuetruth", "--format", "json", "zoo", "--data", "d"])
    assert (e.value.code, e.value.to_stderr) == (2, False)
    doc = json.loads(e.value.text)
    assert doc["code"] == "QT-USAGE-001" and doc["schema"] == "qt.error"
    with pytest.raises(_core.CliExit) as e:
        _core.cli_parse(["queuetruth", "zoo", "--data", "d"])
    assert (e.value.code, e.value.to_stderr) == (2, True) and e.value.text.startswith("QT-USAGE-001: ")
