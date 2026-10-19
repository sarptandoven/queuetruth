"""SPEC 7.4: the synthetic pipeline (replay, zoo, calibrate, features, fit-hazard, score, referee, report, export) run
twice with `--threads 1` / `OMP_NUM_THREADS=1` and once with `--threads 4` / `OMP_NUM_THREADS=4`, in different
directories: every output file is byte-identical (JSON compared without `timings`, SPEC 7.2)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from e2e_support import run_pipeline, strip_volatile, tree_digests

pytestmark = [pytest.mark.integration, pytest.mark.requires_core]

STAGE_DIRS = ["ds", "replay", "zoo", "cal", "feat", "model", "score", "referee", "report", "export"]


@pytest.fixture(scope="module")
def runs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[Path, dict[str, Any]]]:
    out = {}
    for name, threads, omp in (("a_t1", 1, 1), ("b_t1", 1, 1), ("c_t4", 4, 4)):
        w = tmp_path_factory.mktemp(f"det_{name}")
        out[name] = (w, run_pipeline(w, threads=threads, omp=omp))
    return out


def _digests(w: Path) -> dict[str, dict[str, str]]:
    return {d: tree_digests(w / d) for d in STAGE_DIRS}


def test_run_to_run_identical(runs: dict[str, tuple[Path, dict[str, Any]]]) -> None:
    (wa, da), (wb, db) = runs["a_t1"], runs["b_t1"]
    ga, gb = _digests(wa), _digests(wb)
    assert all(ga[d] for d in STAGE_DIRS), {d: len(ga[d]) for d in STAGE_DIRS}
    diff = {d: sorted(k for k in set(ga[d]) | set(gb[d]) if ga[d].get(k) != gb[d].get(k)) for d in STAGE_DIRS}
    assert not any(diff.values()), diff
    assert strip_volatile(da) == strip_volatile(db)


def test_threads_and_omp_do_not_change_outputs(runs: dict[str, tuple[Path, dict[str, Any]]]) -> None:
    (wa, da), (wc, dc) = runs["a_t1"], runs["c_t4"]
    ga, gc = _digests(wa), _digests(wc)
    diff = {d: sorted(k for k in set(ga[d]) | set(gc[d]) if ga[d].get(k) != gc[d].get(k)) for d in STAGE_DIRS}
    assert not any(diff.values()), diff

    # stdout summaries: equal except the echoed `threads` (execution-only, SPEC 7.3) and timings
    def norm(doc: Any) -> Any:
        if isinstance(doc, dict):
            return {k: norm(v) for k, v in doc.items() if k not in ("threads", "timings")}
        if isinstance(doc, list):
            return [norm(v) for v in doc]
        return doc

    assert norm(da) == norm(dc)
