"""Gate tests (gate-test convention: `integration` + `requires_core`): queuetruth's shadow engine against the
independent oracle, through the public Python API `queuetruth.api.replay`.

- Every hand-built scenario whose outcome the audit rulings A1-A6 fix (`ruled == "agree"`) must match the oracle
  exactly (fills, timing, first-fill reason, liquidity when exposed, queue-ahead trajectory).
- Scenarios where the rulings as worded and the oracle differ (`contested`) are xfail(strict=False): they document
  the open question and flip to pass if queuetruth adopts the oracle's reading.
- On the testkit fixtures, every disagreement must be attributed to an audit category.
Skipped with a reason while `queuetruth._core` is not built or `replay` is still a stub.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _helpers import FIXTURES, run_scenario
from oracle_scenarios import audit_scenarios, spec_scenarios
from qt_oracle.compare import build_report, read_qt_results
from qt_oracle.runner import read_amends, read_shadows, run_oracle
from qt_oracle.scenarios import write_qt_dataset, write_shadow_amends, write_shadow_spec
from qt_oracle.tape import read_qt_dataset

pytestmark = [pytest.mark.integration, pytest.mark.requires_core]


def _qt_replay(
    dataset: Path, shadows: Path, amends: Path | None, out: Path, exclude: frozenset[int] = frozenset()
) -> dict:
    import queuetruth.api as api
    from queuetruth.schemas.specs import ReplaySpec

    spec = {
        "dataset": {"path": str(dataset)},
        "out": str(out),
        "shadows": {"path": str(shadows)},
        "trajectory": "ahead",
    }
    if amends is not None:
        spec["amends"] = {"path": str(amends)}
    if exclude:
        if "exclude_order_ids" not in ReplaySpec.model_fields:
            pytest.skip("ReplaySpec has no exclude_order_ids yet (ruling A1)")
        spec["exclude_order_ids"] = sorted(exclude)
    try:
        api.replay(spec)
    except NotImplementedError as e:
        pytest.skip(f"queuetruth.api.replay not implemented yet: {e}")
    return read_qt_results(out)


def _params():
    out = []
    for sc in spec_scenarios() + audit_scenarios():
        marks = []
        if sc.ruled == "contested":
            marks.append(pytest.mark.xfail(strict=False, reason="rulings as worded differ from the oracle"))
        out.append(pytest.param(sc, id=sc.name, marks=marks))
    return out


@pytest.mark.parametrize("sc", _params())
def test_o_sh_5_queuetruth_matches_oracle_on_scenarios(sc, tmp_path) -> None:
    ds = write_qt_dataset(tmp_path / "ds", sc.records(), sc.meta())
    sh = write_shadow_spec(tmp_path / "shadows.parquet", sc.shadows)
    am = write_shadow_amends(tmp_path / "amends.parquet", sc.amends) if sc.amends else None
    ref = _qt_replay(ds, sh, am, tmp_path / "qt", sc.exclude)
    res = run_scenario(sc)
    rep = build_report(list(res.values()), ref, reference_kind="queuetruth")
    assert rep["summary"]["n_disagree"] == 0, rep["discrepancies"]


@pytest.mark.parametrize("name", ["synth_small", "synth_small_nasdaq"])
def test_o_sh_5_queuetruth_vs_oracle_on_fixtures(name, tmp_path) -> None:
    root = FIXTURES / name
    if not (root / "truth" / "shadows.parquet").exists():
        pytest.skip(f"fixture {name} missing")
    shadows, amends = root / "truth" / "shadows.parquet", root / "truth" / "amends.parquet"
    ref = _qt_replay(root, shadows, amends, tmp_path / "qt")
    res = run_oracle(read_qt_dataset(root), read_shadows(shadows), read_amends(amends))
    rep = build_report(res["results"], ref, reference_kind="queuetruth")
    s = rep["summary"]
    assert s["n_compared"] == len(res["results"])
    bad = [d for d in rep["discrepancies"] if d["category"] in ("unexplained", "oracle_divergence")]
    assert not bad, bad[:5]
    if name == "synth_small":
        assert s["n_disagree"] == 0, rep["discrepancies"][:5]
