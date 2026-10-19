"""queuetruth.export.trust: the exportability rules (trader review D1, D2, E1), the `beats_default` per-horizon
record, `annotate`, the refusal error, and parity of the Python grid diagnostics with `qt_calib::trust` (the same
cases and texts as crates/calib/src/tests.rs)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from queuetruth.export import trust
from queuetruth.schemas.models import CalibrationResult

H1, H10, H60 = "1000000000", "10000000000", "60000000000"


def ho(ll: dict[str, float | None], br: dict[str, float | None]) -> dict:
    return {"logloss": ll, "brier": br, "fill_rate_bias": {}}


def cal(**kw) -> CalibrationResult:
    base = dict(
        run_id="0123456789abcdef",
        spec={},
        kind="alloc",
        family="power3",
        mode="faithful",
        param_name="n",
        estimate=1.5,
        ci_bootstrap=(1.2, 1.8),
        n_events=500,
        n_shadows=40,
        train={"dates": ["2026-06-15"]},
        test={"dates": ["2026-06-16"]},
        held_out={"hbt_power3[n=1.5]": ho({H1: 0.1, H10: 0.2}, {H1: 0.05, H10: 0.08})},
        baseline_held_out={"hbt_power3[n=2]": ho({H1: 0.1, H10: 0.3}, {H1: 0.05, H10: 0.09})},
        stratified=[],
        exports={},
        data_sources=[],
        warnings=[],
    )
    base.update(kw)
    return CalibrationResult.model_validate(base)


def test_exportable_fit_beats_default_with_ties_allowed():
    t = trust.assess(cal())
    assert t.ok and t.reasons == []
    assert [(h.horizon_ns, h.label) for h in t.horizons] == [(10**9, "tie"), (10**10, "better")]
    assert t.beats_default == {H1: False, H10: True}
    assert trust.trust_lines(t) == [
        "trust: beats_default: 1000000000=tie,10000000000=better (beats the default hbt_power3[n=2] out of sample: "
        "better at 10s; tie at 1s)",
        "trust: verdict: exportable",
    ]


@pytest.mark.parametrize(
    ("fit", "default", "label"),
    [
        ((0.2, 0.08), (0.3, 0.09), "better"),
        ((0.3, 0.09), (0.3, 0.09), "tie"),
        ((0.3, 0.1), (0.2, 0.09), "worse"),
        ((0.2, 0.1), (0.3, 0.09), "mixed"),
        ((0.2, 0.09), (0.3, 0.09), "better"),
        ((None, 0.08), (0.3, 0.09), "better"),
        ((None, None), (0.3, 0.09), "n/a"),
    ],
)
def test_horizon_labels(fit, default, label):
    c = cal(
        held_out={"f": ho({H10: fit[0]}, {H10: fit[1]})},
        baseline_held_out={"d": ho({H10: default[0]}, {H10: default[1]})},
    )
    t = trust.assess(c)
    assert t.horizons[0].label == label
    assert t.ok is (label == "better")


def test_review_d2_fit_that_loses_out_of_sample_is_refused():
    """Trader review D2: n = 0.986 has held-out log loss 0.0277 / 0.1022 vs 0.0139 / 0.0876 for the default n = 2."""
    c = cal(
        estimate=0.986,
        held_out={"hbt_power3[n=0.986]": ho({H10: 0.0277, "300000000000": 0.1022}, {})},
        baseline_held_out={"hbt_power3[n=2]": ho({H10: 0.0139, "300000000000": 0.0876}, {})},
    )
    t = trust.assess(c)
    assert not t.ok and [k for k, _ in t.reasons] == ["not_beating_default"]
    assert "WORSE at 10s, 5m" in t.reasons[0][1]
    assert trust.trust_lines(t)[-1] == "trust: verdict: refused (not_beating_default)"


def test_review_e1_tie_everywhere_is_refused():
    """Trader review E1 (MBO tape): held-out log loss identical for fitted and default (0.17056): the fit changes
    nothing, so it does not beat the default."""
    c = cal(held_out={"f": ho({H60: 0.17056}, {H60: 0.1})}, baseline_held_out={"d": ho({H60: 0.17056}, {H60: 0.1})})
    t = trust.assess(c)
    assert [k for k, _ in t.reasons] == ["not_beating_default"]


def test_review_d1_degenerate_fit_is_refused_from_its_numbers_alone():
    """Trader review D1, a result written before `trust:` lines existed: zero events, grid-minimum estimate,
    zero-width CI, no held-out metrics."""
    c = cal(
        estimate=math.exp(-3),
        n_events=0,
        n_shadows=0,
        ci_bootstrap=(math.exp(-3), math.exp(-3)),
        held_out={},
        baseline_held_out={},
        spec={"grid": {"alloc": {"lo": -3.0, "hi": 4.0, "n": 141}}},
    )
    t = trust.assess(c)
    assert [k for k, _ in t.reasons] == ["few_events", "at_grid_edge", "degenerate_ci", "no_held_out"]


def test_flags_from_warnings_refuse_and_notes_do_not():
    for tok in ("at_grid_edge", "flat_likelihood", "not_identified", "degenerate_ci"):
        t = trust.assess(cal(warnings=[f"trust: {tok}: something"]))
        assert [k for k, _ in t.reasons] == [tok], tok
    for tok in ("plateau", "profile_ci_open", "nonfinite", "ci_grid_resolution"):
        assert trust.assess(cal(warnings=[f"trust: {tok}: something"])).ok, tok
    assert [k for k, _ in trust.assess(cal(estimate=None)).reasons] == ["no_estimate"]
    assert [k for k, _ in trust.assess(cal(n_events=29)).reasons] == ["few_events"]


def test_latency_calibrations_need_samples_only():
    def lat_cal(entry_n: int) -> CalibrationResult:
        q = {"p10": 1.0, "p50": 2.0, "p90": 3.0, "p99": 4.0, "n": 5}
        lat = {k: q for k in ("response_ns", "cancel_ns", "amend_ns")}
        entry = {**q, "n": entry_n, "p50": 2.0 if entry_n else None}
        return cal(
            kind="latency",
            family="latency",
            estimate=None,
            held_out={},
            baseline_held_out={},
            n_events=entry_n,
            latency={**lat, "entry_ns": entry, "clock_sync_assumed": True},
        )

    assert trust.assess(lat_cal(5)).ok
    assert [k for k, _ in trust.assess(lat_cal(0)).reasons] == ["no_estimate"]


def test_annotate_is_idempotent_and_refusal_error():
    c = trust.annotate(trust.annotate(cal(warnings=["x", "trust: plateau: p"])))
    assert c.warnings[:2] == ["x", "trust: plateau: p"]
    assert sum(w.startswith("trust: verdict:") for w in c.warnings) == 1
    assert sum(w.startswith("trust: beats_default:") for w in c.warnings) == 1
    assert trust.refusal(c, "hftbacktest") is None
    e = trust.refusal(cal(n_events=3), "nautilus")
    assert e is not None and e.code == "QT-CALIB-001" and e.exit_code == 1
    assert e.details["reasons"] == ["few_events"] and e.details["beats_default"] == {H1: False, H10: True}
    assert str(e).startswith("QT-CALIB-001: refusing to export calibration 0123456789abcdef (power3) to nautilus")


def test_dur():
    assert [trust.dur(x) for x in (100_000_000, 10**9, 60 * 10**9, 300 * 10**9, 1500, 7)] == [
        "100ms",
        "1s",
        "60s",
        "5m",
        "1500ns",
        "7ns",
    ]


# ---- parity with qt_calib::trust (same cases and texts as crates/calib/src/tests.rs) ---------------------------------


def grid(lo: float, hi: float, n: int) -> list[float]:
    return [float(x) for x in np.linspace(lo, hi, n)]


def test_grid_lines_well_identified_quadratic():
    th = grid(-3.0, 4.0, 141)
    assert trust.grid_lines(th, [-50.0 * (t - 0.7) ** 2 for t in th], "n", math.exp) == []


def test_grid_lines_match_rust_texts():
    th = grid(-3.0, 4.0, 141)
    lines = trust.grid_lines(th, [10.0 * t for t in th], "n", math.exp)
    assert any(x.startswith("trust: at_grid_edge: the maximum is at the upper end") for x in lines)
    # Review E1 (L2 tape): flat over the first 13 of 29 grid points.
    th = grid(-3.0, 4.0, 29)
    ll = [-54.5 if i < 13 else -54.5 - (i - 12) for i in range(29)]
    lines = trust.grid_lines(th, ll, "n", math.exp)
    assert lines[0] == (
        "trust: at_grid_edge: the maximum is at the lower end of the grid (n = 0.0497871; grid n in [0.0497871, "
        "54.5982]); the likelihood may keep rising outside it"
    )
    assert lines[1].startswith("trust: plateau: the likelihood ties over 13 grid points, n in [0.0497871, ")
    # Review E1 (MBO tape): narrow interior plateau, reported but not flat.
    ll = [-10.0 if 6 <= i <= 9 else -10.0 - 30.0 * abs(i - 7.5) for i in range(29)]
    lines = trust.grid_lines(th, ll, "n", math.exp)
    assert len(lines) == 1 and lines[0].startswith("trust: plateau: the likelihood ties over 4 grid points")
    # Weak curvature: the profile region holds the whole grid.
    th = grid(-3.0, 4.0, 141)
    lines = trust.grid_lines(th, [-0.01 * (t - 0.5) ** 2 for t in th], "n", math.exp)
    assert any(x.startswith("trust: flat_likelihood: the 95% profile region holds every grid") for x in lines)
    # Curvature below the threshold with a closed profile: I = 0.6, half-width 2.53 >= 7 / 4.
    lines = trust.grid_lines(th, [-0.3 * (t - 0.5) ** 2 for t in th], "n", math.exp)
    assert any("curvature at the maximum" in x for x in lines)
    # Constant: not identified (and at the edge).
    th = grid(-3.0, 4.0, 29)
    lines = trust.grid_lines(th, [-3.5] * 29, "n", math.exp)
    assert lines[0].startswith("trust: not_identified:") and lines[1].startswith("trust: at_grid_edge:")
    # One open side.
    th = grid(-3.0, 4.0, 141)
    lines = trust.grid_lines(th, [-((t + 2.5) ** 2) for t in th], "n", math.exp)
    want = "trust: profile_ci_open: the 95% profile region reaches the lower grid end"
    assert any(x.startswith(want) for x in lines)


# ---- structured `trust` field (schema 1.1) -------------------------------------------------------------------


def test_annotate_sets_the_structured_field_and_replaces_the_rust_part():
    rust = {"verdict": "refused", "reasons": ["no_held_out"], "flags": ["plateau"], "beats_default": {}}
    c = trust.annotate(cal(trust=rust, warnings=["trust: plateau: ties over 3 grid points"]))
    assert c.trust is not None and c.trust.verdict == "exportable" and c.trust.reasons == []
    assert c.trust.flags == ["plateau"] and c.trust.beats_default == {H1: "tie", H10: "better"}
    bad = trust.annotate(cal(n_events=3, warnings=["trust: at_grid_edge: lower end"]))
    assert bad.trust is not None and bad.trust.reasons == ["few_events", "at_grid_edge"]
    assert bad.warnings[-1] == "trust: verdict: refused (few_events, at_grid_edge)"


def test_export_reads_the_structured_field_first():
    # Refused by the field although the lines and numbers alone would pass; texts fall back to generic ones.
    st = {"verdict": "refused", "reasons": ["own_orders_in_depth"], "flags": ["own_orders_in_depth"],
          "beats_default": {H10: "better"}}  # fmt: skip
    t = trust.assess(cal(trust=st))
    assert [k for k, _ in t.reasons] == ["own_orders_in_depth"] and "own resting orders" in t.reasons[0][1]
    assert t.beats_default == {H10: True}
    # Exportable by the field: the verdict wins over the recomputation (no held-out metrics here).
    ok = trust.assess(cal(held_out={}, baseline_held_out={}, trust={**st, "verdict": "exportable", "reasons": []}))
    assert ok.ok
    # Without the field, the lines decide (back-compatibility).
    assert not trust.assess(cal(warnings=["trust: own_orders_in_depth: kept"])).ok


def test_spec_force_field(tmp_path):
    from queuetruth.export import ExportRefused, run_export

    (tmp_path / "calibration.json").write_text(cal(n_events=3).model_dump_json())
    spec = {"target": "nautilus", "calibration": {"path": str(tmp_path)}, "out": str(tmp_path / "nt.py")}
    with pytest.raises(ExportRefused):
        run_export(spec)
    with pytest.warns(UserWarning):
        s = run_export({**spec, "force": True})
    assert "# !!   - few_events:" in (s.text or "")
