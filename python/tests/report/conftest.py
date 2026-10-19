"""Fixtures for the report tests: one full synthetic input set (scorecard, zoo, calibration, learned model,
replay) built by `report.synth`, and the report rendered over it."""

from __future__ import annotations

from typing import Any

import pytest

from report.synth import Bundle, make_bundle


@pytest.fixture(scope="session")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> Bundle:
    """A full synthetic input set (shared, read-only)."""
    return make_bundle(tmp_path_factory.mktemp("report_inputs"))


@pytest.fixture(scope="session")
def rendered(bundle: Bundle, tmp_path_factory: pytest.TempPathFactory) -> tuple[Any, str]:
    """The report over the full bundle: (summary, html text)."""
    from queuetruth.report import render

    out = tmp_path_factory.mktemp("report_out") / "report.html"
    summary = render({"inputs": bundle.inputs, "out": str(out), "title": "Synthetic report", "max_why": 6})
    return summary, out.read_text(encoding="utf-8")
