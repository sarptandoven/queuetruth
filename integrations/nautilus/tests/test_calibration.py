"""Reading calibrations and export snippets (no nautilus_trader needed)."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest
from conftest import FIXTURES, exportable, variant, write

import queuetruth_nautilus as qtn
from queuetruth_nautilus.calibration import family_key, window_text


def test_real_touch_calibration(touch_doc):
    cal = qtn.from_document(touch_doc, "cal_touch.json")
    assert cal.run_id == "8fd2495564b974f8"
    assert cal.family == "nt_prob_touch"
    assert cal.kind == "nt_touch"
    assert cal.mode == "faithful"
    assert cal.prob_fill_on_limit == pytest.approx(0.28662597114317423, abs=0)
    assert cal.queue_position is False
    assert cal.latency is None
    assert cal.trust.ok and cal.trust.source == "structured"
    assert cal.trust.beats_default[1_000_000_000] == "better"
    assert cal.n_events == 3604 and cal.n_shadows == 1536
    assert cal.train == "2019-12-30 blocks 0-3/5"
    assert cal.test == "2020-01-30"
    assert cal.held_out is not None and cal.baseline_held_out is not None
    assert cal.held_out.fill_rate_bias[1_000_000_000] == pytest.approx(0.004112192665570474)
    assert cal.baseline_held_out.fill_rate_bias[1_000_000_000] == pytest.approx(0.028)
    assert cal.baseline_held_out.model_id == "nt_prob_touch[mode=faithful;p=1]"
    assert cal.fits_fill_model
    assert cal.data_sources[0]["validation_status"] == "real_data"


def test_real_queue_calibration_is_refused(queue_doc):
    cal = qtn.from_document(queue_doc)
    assert cal.family == "nt_queue_l2_prob_touch"
    assert cal.queue_position is True
    assert cal.prob_fill_on_limit == pytest.approx(0.8711256117455138)
    assert not cal.trust.ok
    assert cal.trust.reasons == ("not_beating_default",)
    with pytest.raises(qtn.CalibrationRefused) as ei:
        qtn.check_trust(cal)
    assert ei.value.code == "QT-CALIB-001" and ei.value.exit_code == 1
    assert ei.value.reasons == ["not_beating_default"]


def test_force_turns_refusal_into_warning(queue_doc):
    cal = qtn.from_document(queue_doc)
    with pytest.warns(qtn.CalibrationWarning, match="NOT VALIDATED"):
        qtn.check_trust(cal, force=True)


def test_load_file_and_directory(tmp_path, touch_doc):
    p = write(tmp_path, touch_doc)
    assert qtn.load_calibration(p).run_id == "8fd2495564b974f8"
    assert qtn.load_calibration(tmp_path).source_path == str(p)
    d2 = tmp_path / "fitdir"
    d2.mkdir()
    write(d2, touch_doc, "fit.json")
    assert qtn.load_calibration(d2).run_id == "8fd2495564b974f8"


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (None, "QT-INPUT-001"),
        ("{not json", "QT-INPUT-002"),
        ('{"schema": "qt.zoo_summary", "schema_version": 1}', "QT-INPUT-002"),
        ("hello", "QT-INPUT-002"),
        ("# queuetruth 0.1.1 | calibration abc | hftbacktest\nfoo", "QT-INPUT-002"),
    ],
)
def test_load_errors(tmp_path, content, code):
    p = tmp_path / "x.json"
    if content is not None:
        p.write_text(content)
    with pytest.raises(qtn.QtNautilusError) as ei:
        qtn.load_calibration(p)
    assert ei.value.code == code
    assert ei.value.exit_code == 2
    assert str(ei.value).startswith(code + ": ")


def test_empty_directory(tmp_path):
    with pytest.raises(qtn.QtNautilusError, match=r"no calibration\.json") as ei:
        qtn.load_calibration(tmp_path)
    assert ei.value.code == "QT-INPUT-001"


def test_unreadable_bytes(tmp_path):
    p = tmp_path / "c.json"
    p.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(qtn.QtNautilusError) as ei:
        qtn.load_calibration(p)
    assert ei.value.code == "QT-INPUT-001"


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"schema_version": 2}, "schema_version"),
        ({"run_id": ""}, "run_id"),
        ({"estimate": 1.5}, r"not a probability"),
        ({"estimate": -0.01}, r"not a probability"),
        ({"estimate": "0.3"}, "expected a number"),
        ({"estimate": True}, "expected a number"),
        ({"latency": [1, 2]}, "latency"),
        ({"trust": "exportable"}, "trust"),
        ({"data_sources": {"a": 1}}, "data_sources"),
        ({"held_out": {"m": {"brier": {"1000": "x"}}}}, "not a number"),
        ({"ci_bootstrap": 0.3}, "ci_bootstrap"),
        ({"ci_bootstrap": 5}, "ci_bootstrap"),
        ({"ci_bootstrap": [0.1]}, "ci_bootstrap"),
        ({"ci_bootstrap": [0.1, "x"]}, "ci_bootstrap"),
        ({"ci_bootstrap": [True, 0.3]}, "ci_bootstrap"),
        ({"n_events": "abc"}, "n_events"),
        ({"n_events": -1}, "n_events"),
        ({"n_events": True}, "n_events"),
        ({"n_shadows": 1.5}, "n_shadows"),
        ({"trust": {"verdict": "exportable", "reasons": 5}}, "trust.reasons"),
        ({"trust": {"verdict": "refused", "reasons": [1]}}, "trust.reasons"),
        ({"trust": {"verdict": "exportable", "flags": "x"}}, "trust.flags"),
        ({"trust": {"verdict": "exportable", "beats_default": {"1s": "better"}}}, "beats_default"),
        ({"trust": {"verdict": "exportable", "beats_default": {"1000": 3}}}, "beats_default"),
        ({"trust": {"verdict": "exportable", "beats_default": ["better"]}}, "beats_default"),
        ({"trust": {"verdict": "maybe"}}, "trust.verdict"),
        ({"trust": {}}, "trust.verdict"),
        ({"train": {"dates": None, "blocks": {"index": [0]}}}, "train"),
    ],
)
def test_malformed_documents(touch_doc, changes, match):
    with pytest.raises(qtn.QtNautilusError, match=match) as ei:
        qtn.from_document(variant(touch_doc, **changes))
    assert ei.value.code == "QT-INPUT-002"


def test_unknown_structured_verdict_does_not_defer_to_warning_lines(touch_doc):
    """The structured field wins: an unrecognised verdict is an error even when an older `trust: verdict:
    exportable` line is present."""
    d = variant(touch_doc, trust={"verdict": "maybe"})
    d["warnings"] = [*d["warnings"], "trust: verdict: exportable"]
    with pytest.raises(qtn.QtNautilusError, match=r"trust\.verdict") as ei:
        qtn.from_document(d)
    assert ei.value.code == "QT-INPUT-002"


def test_valid_optional_fields(touch_doc):
    cal = qtn.from_document(variant(touch_doc, ci_bootstrap=[None, 0.3], n_events=None, n_shadows=0))
    assert cal.ci_bootstrap == (None, 0.3) and cal.n_events is None and cal.n_shadows == 0
    assert qtn.from_document(variant(touch_doc, ci_bootstrap=None)).ci_bootstrap is None


def test_non_finite_estimate_fits_nothing(touch_doc):
    cal = qtn.from_document(variant(touch_doc, estimate=math.nan))
    assert cal.prob_fill_on_limit is None
    cal = qtn.from_document(variant(touch_doc, estimate=None))
    assert cal.prob_fill_on_limit is None
    with pytest.raises(qtn.QtNautilusError, match="fits no NautilusTrader prob_fill_on_limit") as ei:
        qtn.fill_model_kwargs(cal)
    assert ei.value.code == "QT-CONFIG-001"


def test_boundary_probabilities(touch_doc):
    assert qtn.from_document(variant(touch_doc, estimate=0.0)).prob_fill_on_limit == 0.0
    assert qtn.from_document(variant(touch_doc, estimate=1)).prob_fill_on_limit == 1.0


@pytest.mark.parametrize("family", ["hbt_power3", "databento", "power2", "latency"])
def test_other_families_fit_no_fill_probability(touch_doc, family):
    cal = qtn.from_document(variant(touch_doc, family=family, estimate=3.1))
    assert cal.prob_fill_on_limit is None
    assert cal.queue_position is False


def test_family_key():
    assert family_key(" HBT_Power3 ") == "power3"
    assert family_key("databento_mc") == "databento"
    assert family_key("nt_prob_touch") == "nt_prob_touch"


def test_window_text():
    assert window_text({"dates": ["2020-01-30", "2020-01-31"], "blocks": None}) == "2020-01-30,2020-01-31"
    assert window_text({"dates": None, "blocks": {"index": [0, 1, 2, 4], "n_blocks": 5}}) == "blocks 0-2,4/5"
    assert window_text({}) == "all data"
    assert window_text(None) == "all data"


def test_latency_mapping(latency_doc):
    cal = qtn.from_document(latency_doc)
    assert cal.latency is not None
    assert cal.latency.insert_ns == 250400  # round(250400.4)
    assert cal.latency.update_ns is None  # amend has no samples
    assert cal.latency.cancel_ns == 180000
    assert cal.latency.fitted
    assert qtn.latency_model_kwargs(cal) == {
        "base_latency_nanos": 0,
        "insert_latency_nanos": 250400,
        "update_latency_nanos": 0,
        "cancel_latency_nanos": 180000,
    }


def test_latency_without_samples_is_not_fitted(latency_doc):
    d = variant(latency_doc)
    for k in ("entry_ns", "cancel_ns", "amend_ns"):
        d["latency"][k]["n"] = 0
    cal = qtn.from_document(d)
    assert cal.latency is not None and not cal.latency.fitted
    assert qtn.latency_model_kwargs(cal) is None


def test_negative_latency_clamped_to_zero(latency_doc):
    d = variant(latency_doc)
    d["latency"]["entry_ns"]["p50"] = -5.0
    assert qtn.from_document(d).latency.insert_ns == 0


def test_trust_from_legacy_warning_lines(touch_doc):
    d = variant(touch_doc, trust=None)
    cal = qtn.from_document(d)
    assert cal.trust.source == "warnings" and cal.trust.ok
    d["warnings"] = [*d["warnings"], "trust: verdict: refused (few_events, at_grid_edge)"]
    cal = qtn.from_document(d)
    assert not cal.trust.ok
    assert cal.trust.reasons == ("few_events", "at_grid_edge")


def test_no_verdict_without_queuetruth_is_refused(touch_doc, monkeypatch):
    d = variant(touch_doc, trust=None, warnings=[])
    monkeypatch.setitem(sys.modules, "queuetruth", None)
    monkeypatch.setitem(sys.modules, "queuetruth.export", None)
    cal = qtn.from_document(d)
    assert cal.trust.verdict == "refused"
    assert cal.trust.reasons == ("no_trust_verdict",)
    assert cal.trust.source == "none"
    with pytest.raises(qtn.CalibrationRefused, match="no_trust_verdict"):
        qtn.check_trust(cal)


def test_no_verdict_assessed_by_queuetruth_when_installed(touch_doc):
    pytest.importorskip("queuetruth.export.trust")
    d = variant(touch_doc, trust=None, warnings=[])
    cal = qtn.from_document(d)
    assert cal.trust.source == "queuetruth"
    assert cal.trust.ok  # beats the default at every horizon, 3604 events


def test_summary_is_json(touch_doc, latency_doc, queue_doc):
    for doc in (touch_doc, latency_doc, queue_doc):
        s = qtn.from_document(doc).summary()
        assert json.loads(json.dumps(s)) == s
    s = qtn.from_document(touch_doc).summary()
    assert s["prob_fill_on_limit"] == 0.28662597114317423
    assert s["trust"]["beats_default"]["1000000000"] == "better"
    assert list(s["held_out"]["brier"]) == sorted(s["held_out"]["brier"], key=int)


def test_exportable_helper_crafts_exportable(touch_doc):
    assert qtn.from_document(exportable(touch_doc, estimate=1.0)).trust.ok


# ---- snippets -----------------------------------------------------------------------------------------------------


def test_snippet_touch():
    cal = qtn.load_calibration(FIXTURES / "cal_touch.export.txt")
    assert cal.source_format == "nautilus_snippet"
    assert cal.run_id == "8fd2495564b974f8"
    assert cal.queuetruth_version == "0.1.0"
    assert cal.family == "nt_prob_touch"
    assert cal.prob_fill_on_limit == 0.286626  # the export prints 6 significant digits
    assert cal.queue_position is False
    assert cal.trust.ok and cal.trust.source == "snippet"
    assert cal.n_events == 3604 and cal.n_shadows == 1536
    assert cal.ci_bootstrap == (0.269047, 0.302945)
    assert cal.train == "2019-12-30" and cal.test == "2020-01-30"
    assert cal.latency is None
    assert cal.held_out is None
    assert cal.mode == "faithful"  # from the baseline the export names: nt_prob_touch[mode=faithful;p=1]


def test_snippet_queue_refused():
    cal = qtn.load_calibration(FIXTURES / "cal_queue_refused.export.txt")
    assert cal.queue_position is True
    assert cal.prob_fill_on_limit == 0.871126
    assert not cal.trust.ok
    assert cal.trust.reasons == ("not_beating_default",)
    assert "DOES NOT BEAT" in cal.trust.texts["not_beating_default"]
    with pytest.raises(qtn.CalibrationRefused, match="DOES NOT BEAT"):
        qtn.check_trust(cal)


def test_snippet_latency_reads_delete_keyword():
    """queuetruth 0.1.1 writes `delete_latency_nanos`, a keyword neither NautilusTrader release accepts."""
    text = (FIXTURES / "cal_touch_latency.export.txt").read_text()
    assert "delete_latency_nanos=180000" in text
    cal = qtn.from_snippet(text)
    assert cal.run_id == "000000000000a7e1"
    assert cal.latency is not None
    assert (cal.latency.insert_ns, cal.latency.update_ns, cal.latency.cancel_ns) == (250400, None, 180000)
    assert qtn.latency_model_kwargs(cal)["cancel_latency_nanos"] == 180000


def test_snippet_json_and_text_agree():
    j = qtn.load_calibration(FIXTURES / "cal_touch.json")
    s = qtn.load_calibration(FIXTURES / "cal_touch.export.txt")
    assert s.prob_fill_on_limit == float(format(j.prob_fill_on_limit, ".6g"))
    assert (s.run_id, s.family, s.queue_position, s.trust.verdict) == (
        j.run_id,
        j.family,
        j.queue_position,
        "exportable",
    )


@pytest.mark.parametrize(
    ("edit", "match"),
    [
        (lambda t: t.replace("prob_fill_on_limit=0.286626", "prob_fill_on_limit=1.7"), "not a probability"),
        (lambda t: t.replace("prob_fill_on_limit=0.286626", "prob_fill_on_limit=abc"), "not a number"),
        (lambda t: "\n".join(ln for ln in t.splitlines() if "fitted on" not in ln), "fitted on"),
    ],
)
def test_snippet_errors(edit, match):
    text = edit((FIXTURES / "cal_touch.export.txt").read_text())
    with pytest.raises(qtn.QtNautilusError, match=match) as ei:
        qtn.from_snippet(text)
    assert ei.value.code == "QT-INPUT-002"


def test_snippet_edited_base_latency_refused():
    text = (
        (FIXTURES / "cal_touch_latency.export.txt").read_text().replace("base_latency_nanos=0", "base_latency_nanos=5")
    )
    with pytest.raises(qtn.QtNautilusError, match="base_latency_nanos"):
        qtn.from_snippet(text)


def test_snippet_without_fill_line(tmp_path: Path):
    text = (FIXTURES / "cal_touch.export.txt").read_text()
    text = text.replace("fill_model = FillModel(prob_fill_on_limit=0.286626)\n", "")
    cal = qtn.from_snippet(text)
    assert cal.prob_fill_on_limit is None
