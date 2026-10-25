"""Session fixtures: the committed prediction-market fixtures run through `pm ingest`, `pm calibrate` and `pm score`
once per session (default settings, as the how-to runs them)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pm._helpers import FIX

from queuetruth.pm.calibrate import calibrate
from queuetruth.pm.ingest import ingest
from queuetruth.pm.models import PmCalibration, PmIngestSummary, PmScore
from queuetruth.pm.score import score


@dataclass
class Run:
    """One venue's pipeline outputs."""

    root: Path
    ingest: PmIngestSummary
    calibration: PmCalibration
    score: PmScore
    truth: dict[str, Any]

    @property
    def pm(self) -> Path:
        return self.root / "in"


def _inputs(venue: str) -> tuple[list[Path], list[Path]]:
    if venue == "kalshi":
        return [FIX / "kalshi" / "ws.jsonl", FIX / "kalshi" / "rest.jsonl"], []
    return (
        [FIX / "polymarket" / "market.jsonl", FIX / "polymarket" / "user.jsonl", FIX / "polymarket" / "gamma.json"],
        [FIX / "polymarket" / "logs.json"],
    )


def run_pipeline(venue: str, root: Path) -> Run:
    """ingest -> calibrate -> score (the calibrated model) of a fixture venue under `root`."""
    inputs, chain = _inputs(venue)
    ing = ingest(
        {
            "venue": venue,
            "inputs": [{"path": str(p)} for p in inputs],
            "chain": [{"path": str(p)} for p in chain],
            "out": str(root / "in"),
        }
    )
    cal = calibrate({"pm": {"path": str(root / "in")}, "out": str(root / "cal")})
    sc = score({"pm": {"path": str(root / "in")}, "calibration": {"path": str(root / "cal")}, "out": str(root / "sc")})
    truth = json.loads((FIX / venue / "truth.json").read_text(encoding="utf-8"))
    return Run(root, ing, cal, sc, truth)


@pytest.fixture(scope="session")
def kalshi_run(tmp_path_factory: pytest.TempPathFactory) -> Run:
    return run_pipeline("kalshi", tmp_path_factory.mktemp("pm_kalshi"))


@pytest.fixture(scope="session")
def poly_run(tmp_path_factory: pytest.TempPathFactory) -> Run:
    return run_pipeline("polymarket", tmp_path_factory.mktemp("pm_poly"))
