"""`queuetruth dq` through the CLI and the Python API: the golden parsed commands validate as `DqSpec`, the
dispatcher returns the report and the gate (`--fail-on`), the human view leads with the answer, and the console
script exits 0 / 1 / 2. The console-script tests need a `queuetruth._core` built with the `dq` grammar (`make
develop`); they skip on an older build."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from dq.helpers import FIXTURES, REPO, Cols, edit_partition
from queuetruth import api, cli
from queuetruth.dq import DqReport, DqSpec


def _golden_dq() -> list[dict[str, Any]]:
    cases = json.loads((REPO / "crates" / "cli" / "tests" / "golden" / "cli_parse.json").read_text())
    return [c for c in cases if c["command"]["path"] == ["dq"]]


def _cmd(args: dict[str, Any], fmt: str = "human") -> dict[str, Any]:
    return {
        "path": ["dq"],
        "global": {"format": fmt, "log_level": "warn", "threads": 1, "seed": 0, "quiet": False},
        "args": args,
        "implementation": "python",
    }


def _core_knows_dq() -> bool:
    try:
        from queuetruth import _core
    except ImportError:
        return False
    try:
        _core.cli_parse(["queuetruth", "dq", "--data", "d", "--out", "o"])
    except Exception:
        return False
    return True


needs_dq_grammar = pytest.mark.skipif(
    not _core_knows_dq(), reason="queuetruth._core predates the dq grammar (run `make develop`)"
)


def test_golden_dq_commands_validate_as_specs() -> None:
    cases = _golden_dq()
    assert len(cases) == 2
    flags = {w for c in cases for w in c["argv"] if w.startswith("--")}
    for f in DqSpec.model_fields:
        if f in ("dataset",):
            continue
        flag = {"replay": "--no-replay"}.get(f, "--" + f.replace("_", "-"))
        assert flag in flags, f"no golden case sets {f} ({flag})"
    for case in cases:
        sp = DqSpec.model_validate(case["command"]["args"])
        assert sp.dataset.path
    full = DqSpec.model_validate(cases[1]["command"]["args"])
    assert (full.fail_on, full.replay, full.verify_sources, full.stale_ns) == ("fail", False, True, 30 * 10**9)


def _warned(ds: Path) -> None:
    def zero(c: Cols) -> Cols:
        c["size"][np.nonzero(c["action"] == ord("A"))[0][10]] = 0
        return c

    edit_partition(ds, "2026-06-15", 1, zero)


def test_dispatch_returns_report_and_gate(nasdaq: Path, tmp_path: Path) -> None:
    args = {"dataset": {"path": str(nasdaq)}, "out": str(tmp_path / "ok"), "replay": False}
    doc, gate = cli._dispatch(_cmd(args))
    assert doc["schema"] == "qt.dq_report" and doc["status"] == "pass" and gate is False
    text = cli.render(["dq"], doc, args=args)
    assert text.startswith("data quality: PASS")
    assert "  ok    sequence" in text and "skip  cross_source" in text
    assert str(tmp_path / "ok" / "dq_report.html") in text
    _warned(nasdaq)
    doc, gate = cli._dispatch(_cmd({**args, "out": str(tmp_path / "w")}))
    assert doc["status"] == "warn" and gate is True and doc["gate_failed"] is True
    assert "WARN  size_price" in cli.render(["dq"], doc, args=args)
    _, gate = cli._dispatch(_cmd({**args, "fail_on": "fail"}))
    assert gate is False
    _, gate = cli._dispatch(_cmd({**args, "fail_on": "never"}))
    assert gate is False


def test_api_dq(nasdaq: Path) -> None:
    rep = api.dq({"dataset": {"path": str(nasdaq)}, "checks": ["vintage"]})
    assert isinstance(rep, DqReport) and rep.status == "pass"
    assert "dq" in api.__all__


@needs_dq_grammar
def test_console_script_exit_codes(nasdaq: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = ["dq", "--data", str(nasdaq), "--no-replay", "--format", "json"]
    assert cli.main([*base, "--out", str(tmp_path / "a")]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema"] == "qt.dq_report" and (tmp_path / "a" / "dq_report.html").is_file()
    _warned(nasdaq)
    assert cli.main([*base, "--out", str(tmp_path / "b")]) == 1
    assert "QT-GATE-001" in capsys.readouterr().err
    assert cli.main([*base, "--out", str(tmp_path / "c"), "--fail-on", "fail"]) == 0
    capsys.readouterr()
    assert cli.main(["dq", "--data", str(tmp_path / "missing"), "--out", str(tmp_path / "d"), "--format", "json"]) == 2
    err = json.loads(capsys.readouterr().out)
    assert (err["schema"], err["code"]) == ("qt.error", "QT-INPUT-001")
    assert (
        cli.main(
            ["dq", "--data", str(FIXTURES / "synth_small_nasdaq"), "--out", str(tmp_path / "e"), "--checks", "bogus"]
        )
        == 2
    )
    assert "QT-CONFIG-001" in capsys.readouterr().err
