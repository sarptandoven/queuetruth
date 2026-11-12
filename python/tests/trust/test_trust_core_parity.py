"""The trust rule has one implementation, `qt_calib::trust` (Rust), called by `queuetruth.export.trust`. These tests
check that it gives the same bytes as the Python rule it replaced (`_trust_before.py`, a verbatim copy) on many
synthetic calibrations: the stored `trust:` lines, the structured `trust` field and the refusal reasons."""

from __future__ import annotations

import math

import numpy as np
import pytest

from queuetruth.export import trust
from queuetruth.schemas.models import CalibrationResult
from queuetruth.units import format_duration

from . import _trust_before as before

pytestmark = pytest.mark.requires_core

HORIZONS = [100_000_000, 1_000_000_000, 10_000_000_000, 60_000_000_000, 300_000_000_000, 1_500, 7]
#: Values around every form `.6g` takes: exponents, rounding up to the next power of ten, ties and zeros.
VALUES = [0.1, 0.2, 0.3, 1e-5, 9.999995e-5, 0.0001, 999999.7, 123456789.0, 0.0497870683678639, 2.0, -0.8, 0.0, -0.0]
LINES = [
    "trust: at_grid_edge: the maximum is at the lower end of the grid (n = 0.0497871; grid n in [0.0497871, 54.5982])",
    "trust: flat_likelihood: the 95% profile region holds every grid value; n is not determined by the data",
    "trust: plateau: the likelihood ties over 3 grid points",
    "trust: degenerate_ci: every bootstrap replicate gave n = 2; suppressed",
    "trust: own_orders_in_depth: the user's own resting orders could not be removed",
    "trust: verdict: refused (few_events)",
    "trust: beats_default: 1000000000=better (old)",
    "trust: not_identified: same everywhere\nsecond line",
    "trust: Bad: ignored",
    "plain warning",
]


def _scores(rng: np.random.Generator) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for h in rng.choice(HORIZONS, size=int(rng.integers(0, 4)), replace=False):
        r = rng.random()
        out[str(int(h))] = None if r < 0.15 else float(rng.choice(VALUES)) if r < 0.5 else round(float(rng.random()), 2)
    return out


def _cal(rng: np.random.Generator) -> CalibrationResult:
    family = str(rng.choice(["power3", "hbt_power2", "databento", "HBT_Databento", "nt_prob_touch"]))
    kind = str(rng.choice(["alloc", "alloc", "survival", "nt_touch", "latency"]))
    grid = {"alloc": {"lo": -3.0, "hi": 4.0, "n": 141}, "databento": {"lo": -0.99, "hi": 0.99, "n": 199}}
    est_choice = rng.random()
    estimate = (
        None
        if est_choice < 0.1
        else math.exp(-3.0)
        if est_choice < 0.2
        else -0.99
        if est_choice < 0.3
        else float(rng.choice(VALUES))
        if est_choice < 0.5
        else 1.5
    )
    ci = None if rng.random() < 0.3 else (2.0, 2.0) if rng.random() < 0.3 else (1.2, 1.8)
    held = (
        {} if rng.random() < 0.15 else {"fit": {"logloss": _scores(rng), "brier": _scores(rng), "fill_rate_bias": {}}}
    )
    base = (
        {} if rng.random() < 0.15 else {"dflt": {"logloss": _scores(rng), "brier": _scores(rng), "fill_rate_bias": {}}}
    )
    n_warn = int(rng.integers(0, 4))
    warnings = [str(w) for w in rng.choice(LINES, size=n_warn, replace=False)]
    doc = {
        "run_id": "0123456789abcdef",
        "spec": {"grid": grid} if rng.random() < 0.7 else {},
        "kind": kind,
        "family": family,
        "mode": "faithful",
        "param_name": "n",
        "estimate": estimate,
        "ci_bootstrap": ci,
        "n_events": int(rng.choice([0, 29, 30, 500])),
        "n_shadows": 40,
        "train": {},
        "test": {},
        "held_out": held,
        "baseline_held_out": base,
        "stratified": [],
        "exports": {},
        "data_sources": [],
        "warnings": warnings,
    }
    if kind == "latency" and rng.random() < 0.7:
        q = {"p10": 1.0, "p50": 2.0, "p90": 3.0, "p99": 4.0, "n": int(rng.choice([0, 5]))}
        doc["latency"] = {
            "entry_ns": q,
            "response_ns": q,
            "cancel_ns": q,
            "amend_ns": q,
            "clock_sync_assumed": True,
        }
    return CalibrationResult.model_validate(doc)


def test_annotate_and_assess_match_the_python_rule_they_replaced() -> None:
    rng = np.random.default_rng(20261112)
    for i in range(1500):
        c = _cal(rng)
        try:
            want = before.annotate(c)
        except Exception as e:  # pragma: no cover - the generator only builds valid cases
            raise AssertionError(f"case {i}: the reference failed: {e}") from e
        got = trust.annotate(c)
        assert got.warnings == want.warnings, i
        assert got.trust == want.trust, i
        for fmt in (None, format_duration):
            t, w = trust._compute(c, fmt), before._compute(c, fmt)
            assert (t.flags, t.horizons, t.fitted_id, t.default_id, t.reasons) == (
                w.flags,
                w.horizons,
                w.fitted_id,
                w.default_id,
                w.reasons,
            ), i


@pytest.mark.parametrize("x", [*VALUES, 1e-300, -1e-5, 1e21, 5e-324, 0.5, 1234565.0, 99999.95, math.inf, -math.inf])
def test_g6_is_python_format(x: float) -> None:
    c = CalibrationResult.model_validate(
        {
            "run_id": "0123456789abcdef",
            "spec": {},
            "kind": "alloc",
            "family": "power3",
            "mode": "faithful",
            "estimate": 1.5,
            "n_events": 500,
            "n_shadows": 1,
            "train": {},
            "test": {},
            "held_out": {"f": {"logloss": {"1000000000": 0.9}, "brier": {"1000000000": x}, "fill_rate_bias": {}}},
            "baseline_held_out": {"d": {"logloss": {"1000000000": 0.1}, "brier": {}, "fill_rate_bias": {}}},
            "stratified": [],
            "exports": {},
            "data_sources": [],
            "warnings": [],
        }
    )
    text = dict(trust.assess(c).reasons)["not_beating_default"]
    if math.isfinite(x):
        assert f"Brier {format(x, '.6g')} vs -" in text
