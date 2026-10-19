"""Fail-safe calibration through the real engine on synthetic data written by the same extension (trader review
D1, D2): a train window without data is QT-DATA-001, the result carries the structured `trust` field, and the
export of a real fit either passes the trust assessment or is refused unless forced, with the reasons in the snippet
header. Needs `queuetruth._core` built from the current crates/calib."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import queuetruth as qt
from queuetruth.errors import QtError
from queuetruth.export import ExportRefused, assess, run_export
from queuetruth.schemas import models as m

pytestmark = [pytest.mark.requires_core, pytest.mark.integration]


def _stale(repo_root: Path) -> bool:
    import queuetruth._core as core

    so = Path(core.__file__).stat().st_mtime
    src = max(p.stat().st_mtime for p in (repo_root / "crates" / "calib" / "src").glob("*.rs"))
    return so < src


@pytest.fixture(scope="module")
def synth(repo_root: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A synthetic dataset written by the current extension (`fixtures synth`), so fixture and engine agree."""
    if _stale(repo_root):
        pytest.skip("queuetruth._core predates the crates/calib fail-safe changes: run `make develop`")
    out = tmp_path_factory.mktemp("synth") / "synth"
    from queuetruth import api

    api.fixtures_synth(str(out), "cme", 2)
    return out


@pytest.fixture(scope="module")
def shadows(synth: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    w = tmp_path_factory.mktemp("trust")
    qt.sample_shadows(
        {
            "dataset": {"path": str(synth)},
            "out": str(w / "shadows.parquet"),
            "n": 400,
            "seed": 3,
            "lifetime_ns": 60 * 10**9,
        }
    )
    return w / "shadows.parquet"


def _spec(synth: Path, shadows: Path, out: Path, **kw):
    return {
        "kind": "alloc",
        "family": "power3",
        "dataset": {"path": str(synth)},
        "shadows": {"path": str(shadows)},
        "out": str(out),
        "bootstrap": {"b": 20},
        **kw,
    }


def test_review_d1_train_date_without_data_is_refused(synth: Path, shadows: Path, tmp_path: Path) -> None:
    with pytest.raises(QtError) as e:
        qt.calibrate(
            _spec(synth, shadows, tmp_path / "d1", train={"dates": ["2026-06-17"]}, test={"dates": ["2026-06-16"]})
        )
    assert e.value.code == "QT-DATA-001"
    assert "2026-06-17" in e.value.message and "available: 2026-06-15, 2026-06-16" in e.value.message
    assert not (tmp_path / "d1" / "calibration.json").exists()


def test_real_fit_export_is_gated_by_the_trust_assessment(synth: Path, shadows: Path, tmp_path: Path) -> None:
    out = tmp_path / "cal"
    c = qt.calibrate(_spec(synth, shadows, out, train={"dates": ["2026-06-15"]}, test={"dates": ["2026-06-16"]}))
    disk = m.CalibrationResult.model_validate(json.loads((out / "calibration.json").read_text()))
    t = assess(disk)
    assert t.horizons, "api.calibrate fills held_out / baseline_held_out"
    # Structured trust (schema 1.1): recomputed with the held-out comparison, consistent with the lines.
    assert disk.trust is not None and disk.trust.verdict == ("exportable" if t.ok else "refused")
    assert disk.trust.reasons == [k for k, _ in t.reasons] and disk.trust.beats_default
    assert disk.warnings[-1].startswith("trust: verdict: ")
    # The Rust part's own field (fit.json) cannot see held-out data, so it is refused with `no_held_out`.
    fit = m.CalibrationResult.model_validate(json.loads((out / "fit.json").read_text()))
    assert fit.trust is not None and fit.trust.verdict == "refused" and "no_held_out" in fit.trust.reasons
    assert fit.trust.beats_default == {}
    spec = {"target": "hftbacktest", "calibration": {"path": str(out)}, "out": str(tmp_path / "hbt.py")}
    if t.ok:
        s = run_export(spec)
        assert "NOT VALIDATED" not in s.text
    else:
        with pytest.raises(ExportRefused):
            run_export(spec)
        assert not (tmp_path / "hbt.py").exists()
        s = run_export(spec, force=True)
        assert "# !! NOT VALIDATED" in s.text
        for k, _ in t.reasons:
            assert f"# !!   - {k}:" in s.text
    assert f"{c.n_events} events from {c.n_shadows} shadows" in s.text
    assert "# held-out: " in s.text and "fitted on 2026-06-15; held-out 2026-06-16" in s.text
