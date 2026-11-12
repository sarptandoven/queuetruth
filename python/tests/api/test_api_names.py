"""`queuetruth.api` keeps every name it had before the operations moved to `queuetruth.ops` (the recorded list in
`api_names.txt`), and each operation is defined in its own `queuetruth.ops` module."""

from __future__ import annotations

from pathlib import Path

import queuetruth as qt
from queuetruth import api

RECORDED = [
    line
    for line in (Path(__file__).parent / "api_names.txt").read_text(encoding="utf-8").splitlines()
    if line and not line.startswith("#")
]


def test_api_keeps_every_recorded_name() -> None:
    missing = sorted(set(RECORDED) - set(dir(api)))
    assert not missing, f"queuetruth.api lost {missing}"


def test_public_names_of_the_package_still_resolve() -> None:
    for name in api.__all__:
        assert getattr(api, name) is not None
        if name in qt.__all__ and name not in ("check", "fit", "score", "report", "export"):
            assert getattr(qt, name) is getattr(api, name), name


def test_operations_live_in_ops() -> None:
    where = {
        "ingest": "ingest",
        "replay": "replay",
        "sample_shadows": "shadows",
        "shadows_from_live": "shadows",
        "run_zoo": "zoo",
        "calibrate": "calibrate",
        "calibrate_live": "calibrate",
        "score": "score",
        "import_live": "live",
        "report": "report",
        "dq": "dq",
        "referee": "referee",
        "why_explain": "why",
        "export": "export",
    }
    for name, module in where.items():
        assert getattr(api, name).__module__ == f"queuetruth.ops.{module}", name
