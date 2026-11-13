"""`queuetruth advanced run OP --spec FILE|- [--out DIR]` and `run --list`: any operation of the table from a JSON
spec through `queuetruth.runner.run`, the document as JSON on stdout, errors as `qt.error`. The same specs through
the Rust `run` (`queuetruth-core run`, here through `_core.cli_run_native`) give the same document and the same
error codes."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from queuetruth import cli
from queuetruth.ops.table import table
from queuetruth.runner import run as runner_run
from queuetruth.schemas.specs import SPEC_MODELS

pytestmark = pytest.mark.requires_core


@pytest.fixture(scope="module")
def synth(repo_root: Path) -> Path:
    return repo_root / "tests" / "fixtures" / "synth_small"


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]) -> Any:
    def _run(*argv: str) -> tuple[int, Any, str]:
        code = cli.main(list(argv))
        out = capsys.readouterr()
        text = out.out.strip()
        return code, (json.loads(text) if text.startswith(("{", "[")) else text), out.err

    return _run


def _core_run(op: str, spec: Any, out: str | None = None) -> tuple[int, dict[str, Any]]:
    """The Rust `run` (what `queuetruth-core run` runs) on a spec: (exit code, document or qt.error)."""
    from queuetruth import _core

    path = Path(spec) if isinstance(spec, str) else None
    args: dict[str, Any] = {"operation": op, "spec": str(path)}
    if out is not None:
        args["out"] = out
    cmd = {"path": ["run"], "global": {"format": "json", "log_level": "warn", "threads": 1, "seed": 0, "quiet": False}}
    try:
        text, _ = _core.cli_run_native(json.dumps({**cmd, "args": args, "implementation": "hybrid"}))
    except _core.QtCoreError as e:
        code, message = e.args[0], e.args[1]
        return 2, {"schema": "qt.error", "code": code, "message": message}
    return 0, json.loads(text)


def _same_document(a: dict[str, Any], b: dict[str, Any]) -> None:
    """Equal, but for the fields Pydantic writes empty where the Rust summary leaves them out."""

    def norm(d: Any) -> Any:
        if isinstance(d, dict):
            return {k: norm(v) for k, v in d.items() if v not in (None, [], {})}
        if isinstance(d, list):
            return [norm(v) for v in d]
        return d

    assert norm(a) == norm(b)


def test_shadows_from_a_spec_file_matches_the_runner_and_the_core(run: Any, synth: Path, tmp_path: Path) -> None:
    spec = {"dataset": {"path": str(synth)}, "n": 12, "seed": 7}
    f = tmp_path / "spec.json"
    f.write_text(json.dumps(spec))
    code, doc, _ = run("advanced", "run", "shadows", "--spec", str(f), "--out", str(tmp_path / "cli"))
    assert code == 0, doc
    assert doc["schema"] == "qt.shadow_sample_summary"
    assert (tmp_path / "cli" / "shadows.parquet").is_file()
    expected = runner_run("shadows", spec, out=tmp_path / "py").document
    assert doc["by_instrument"] == expected["by_instrument"]
    code, core_doc = _core_run("shadows", str(f), str(tmp_path / "core"))
    assert code == 0, core_doc
    for d in (doc, expected, core_doc):
        d.pop("files", None)
    _same_document(doc, expected)
    _same_document(doc, core_doc)


def test_spec_from_standard_input(run: Any, synth: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = {"dataset": {"path": str(synth)}, "n": 4, "out": str(tmp_path / "s.parquet")}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(spec)))
    code, doc, _ = run("advanced", "run", "shadows", "--spec", "-")
    assert code == 0, doc
    assert doc["n"] == 4


def test_a_python_operation_runs_through_the_runner(run: Any, synth: Path, tmp_path: Path) -> None:
    f = tmp_path / "dq.json"
    f.write_text(json.dumps({"dataset": {"path": str(synth)}}))
    code, doc, _ = run("advanced", "run", "dq", "--spec", str(f))
    assert code == 0, doc
    assert doc["schema"] == "qt.dq_report"
    # the core says it needs Python
    code, err = _core_run("dq", str(f))
    assert (code, err["code"]) == (2, "QT-USAGE-001")
    assert "requires the Python package" in err["message"]


@pytest.mark.parametrize(
    ("op", "spec", "model", "field"),
    [
        ("shadows", {"dataset": {"path": "ds"}, "n": -3, "bogus": 1}, "ShadowSampleSpec", "n"),
        ("replay", {"dataset": {"path": "ds"}, "bbo": "sometimes"}, "ReplaySpec", "bbo"),
        ("ingest", {"format": "itch", "inputs": [], "venue": "nasdaq"}, "IngestSpec", "inputs"),
        ("score", {}, "ScorecardSpec", None),
    ],
)
def test_an_invalid_spec_is_config_001_on_both_paths(
    run: Any, tmp_path: Path, op: str, spec: dict[str, Any], model: str, field: str | None
) -> None:
    f = tmp_path / "bad.json"
    f.write_text(json.dumps(spec))
    code, doc, _ = run("advanced", "run", op, "--spec", str(f))
    assert code == 2
    assert doc["schema"] == "qt.error"
    assert doc["code"] == "QT-CONFIG-001"
    assert doc["message"].startswith(f"{model}: {field + ': ' if field else ''}")
    code, err = _core_run(op, str(f))
    assert (code, err["code"]) == (2, "QT-CONFIG-001"), err
    assert f"{model}: {field + ': ' if field else ''}" in err["message"]


def test_unknown_operation_and_bad_spec_files(run: Any, tmp_path: Path) -> None:
    f = tmp_path / "spec.json"
    f.write_text("{}")
    code, doc, _ = run("advanced", "run", "nope", "--spec", str(f))
    assert (code, doc["code"]) == (2, "QT-USAGE-001")
    assert "unknown operation 'nope'" in doc["message"]
    code, err = _core_run("nope", str(f))
    assert (code, err["code"]) == (2, "QT-USAGE-001")
    code, doc, _ = run("advanced", "run", "shadows", "--spec", str(tmp_path / "missing.json"))
    assert (code, doc["code"]) == (2, "QT-INPUT-001")
    f.write_text("{not json")
    code, doc, _ = run("advanced", "run", "shadows", "--spec", str(f))
    assert (code, doc["code"]) == (2, "QT-USAGE-001")
    code, doc, _ = run("advanced", "run", "shadows")  # a usage error of the command line is JSON too
    assert (code, doc["code"]) == (2, "QT-USAGE-001")


def test_list_prints_the_table(run: Any) -> None:
    code, doc, _ = run("--format", "json", "advanced", "run", "--list")
    assert code == 0
    ops = table()
    assert [r["name"] for r in doc["operations"]] == list(ops)
    schema_of = {model: name for name, model in SPEC_MODELS.items()}
    for r in doc["operations"]:
        op = ops[r["name"]]
        assert r["description"] == op.description
        assert r["spec_schema"] == schema_of.get(op.spec)
    assert {r["name"] for r in doc["operations"] if r["native"]} == {
        "features",
        "ingest",
        "replay",
        "shadows",
        "shadows_mirror",
    }
    code, text, _ = run("advanced", "run", "--list")
    assert code == 0
    assert text.startswith("operations (queuetruth advanced run NAME --spec FILE")
    assert "qt.replay_spec" in text
