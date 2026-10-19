"""MCP calibration text carries the export verdict, and the metric glossary follows audit rulings A2
(outcome-independent censoring, Aalen-Johansen truth) and A10 (cluster-robust primary discordance test)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from queuetruth.mcp.metrics import METRICS, explain
from queuetruth.mcp.server import render_text, trust_lines

REPO = Path(__file__).resolve().parents[3]


def _calibration(**extra: Any) -> dict[str, Any]:
    doc = json.loads((REPO / "schemas" / "samples" / "qt.calibration_result.json").read_text())["sample"]
    doc = {k: v for k, v in doc.items() if k != "trust"}
    return doc | extra


def test_calibration_text_has_the_trust_lines_from_warnings():
    doc = _calibration(
        warnings=[
            "few shadows",
            "trust: beats_default: 1000000000=better,10000000000=tie (log loss and Brier, held out)",
            "trust: verdict: refused (at_grid_edge)",
            "trust: at_grid_edge: n = 3 is the grid maximum",
        ]
    )
    lines = render_text(doc).splitlines()
    assert lines[0].startswith(f"calibration {doc['run_id']} (")
    assert lines[1] == "trust: beats_default: 1000000000=better,10000000000=tie (log loss and Brier, held out)"
    assert lines[2] == "trust: verdict: refused (at_grid_edge)"


def test_calibration_text_prefers_the_structured_trust_field():
    trust = {
        "verdict": "exportable",
        "reasons": [],
        "flags": [],
        "beats_default": {"10000000000": "better", "1000000000": "tie"},
    }
    doc = _calibration(trust=trust, warnings=["trust: verdict: refused (stale line)"])
    assert trust_lines(doc) == [
        "trust: beats_default: 1000000000=tie,10000000000=better",
        "trust: verdict: exportable",
    ]
    refused = _calibration(trust=trust | {"verdict": "refused", "reasons": ["few_events", "no_held_out"]})
    assert trust_lines(refused)[-1] == "trust: verdict: refused (few_events, no_held_out)"
    assert trust_lines(_calibration(warnings=["no trust lines"])) == []


def test_glossary_follows_rulings_a2_and_a10():
    mc = explain("mcnemar")
    assert mc["notes"].startswith("secondary test, assumes independent orders; the primary discordance test is")
    assert "primary test uses p_exact" not in mc["notes"]
    cl = explain("discordance_clustered")
    assert cl["formula"].startswith("d = (c - b)/N with a cluster-robust p (Obuchowski with t(K-1)")
    assert cl["proper"] is False and "discordance_clustered" in explain("overfill_bias")["notes"]
    brier = explain("brier")["formula"]
    assert "censored rows excluded" not in brier and "whatever the outcome (A2)" in brier
    assert "label-eligible" in explain("logloss")["formula"]
    assert explain("ttf_iad")["formula"].endswith("never 1 - KM") and "KM_truth" not in explain("ttf_iad")["formula"]
    assert "fill_rate_bias_aj" in explain("fill_rate_bias")["formula"]
    assert explain("calib_intercept")["formula"] == explain("calib_slope")["formula"]
    assert "IPCW" in explain("calib_slope")["formula"] and "w = IPCW weights" in explain("spiegelhalter_z")["formula"]
    assert "hl_p_et with 8 df" in explain("hosmer_lemeshow")["formula"]
    assert "8 df" not in explain("hosmer_lemeshow")["definition"]
    assert explain("markout")["notes"].endswith("not NBBO; also in bps of the fill price and net of fees.")
    assert "clustered_mcnemar" not in METRICS
    assert all(m.name == k for k, m in METRICS.items())
