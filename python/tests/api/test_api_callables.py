"""`qt.score`, `qt.report`, `qt.export` stay callable (and dispatch to `queuetruth.api`) whatever the import order,
including submodules imported before `queuetruth`, after it, deep submodules, and a subpackage dropped from
`sys.modules` and imported again; `import queuetruth.report as m` still gives the module."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCENARIOS = {
    "plain": "import queuetruth as qt",
    "submodules_first": "import queuetruth.export.hftbacktest, queuetruth.score.scorecard, queuetruth.report.why\n"
    "import queuetruth as qt",
    "submodules_after": "import queuetruth as qt\nfrom queuetruth.score import build_scorecard\n"
    "from queuetruth.export import run_export\nimport queuetruth.report as rm\nassert callable(rm.render)",
    "api_first": "from queuetruth import api\nfrom queuetruth.score._io import resolve_ref\nimport queuetruth as qt",
    "reimported": "import sys, importlib\nimport queuetruth as qt\n"
    "for k in [k for k in list(sys.modules) if k.split('.')[:2] in (['queuetruth', 'score'], ['queuetruth', 'report'],"
    " ['queuetruth', 'export'])]:\n    del sys.modules[k]\n"
    "importlib.import_module('queuetruth.score.scorecard'); import queuetruth.report.why, queuetruth.export",
}

CHECK = """
from queuetruth.errors import QtError
for name in ("score", "report", "export"):
    f = getattr(qt, name)
    assert callable(f), name
    try:
        f({"no_such_field": 1})
    except QtError as e:
        assert e.code == "QT-CONFIG-001", (name, e.code)
    else:
        raise AssertionError(name)
assert hasattr(qt.score, "build_scorecard") and hasattr(qt.report, "render") and hasattr(qt.export, "run_export")
print("ok")
"""


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_public_callables_survive_import_order(scenario: str, repo_root: Path) -> None:
    code = SCENARIOS[scenario] + "\n" + CHECK
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=repo_root,
        env={"PYTHONPATH": str(repo_root / "python"), "PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert r.returncode == 0 and r.stdout.strip().endswith("ok"), r.stderr


def test_callables_in_process() -> None:
    import queuetruth as qt
    import queuetruth.export
    import queuetruth.report
    import queuetruth.score

    for name in ("score", "report", "export"):
        with pytest.raises(qt.QtError):
            getattr(qt, name)({"no_such_field": 1})
