"""`export_config.py`: calibrations -> qtint.hbt_queue_config, with the trust gate and the log-family comparison.

Fixtures are the real-data calibrations of the documented run (README.md; nasdaq-2d, 2d-sampled-6000 shadows,
train 2019-12-30, held-out 2020-01-30) with the absolute dataset paths made relative.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import export_config as ec
import hbt_config as hc
import pytest

from queuetruth.errors import QtError
from queuetruth.export import load_calibration

FIX = Path(__file__).resolve().parent / "fixtures"
P3 = FIX / "calibration_power3.json"  # trust: exportable
P1 = FIX / "calibration_power.json"  # trust: refused (not_beating_default)


def test_fitted_default_and_log_entries(tmp_path: Path) -> None:
    out = tmp_path / "cfg.json"
    assert ec.main(["--calibration", str(P3), "--log", "--defaults", "--out", str(out)]) == 0
    doc = hc.load(out)
    by_role = {(m["role"], m["family"]): m for m in doc["models"]}
    assert [m["model_id"] for m in doc["models"]] == [
        "hbt_power3[exch=partial;mode=faithful;n=3.165744804349019]",
        "hbt_power3[exch=partial;mode=faithful;n=2]",
        "hbt_log[exch=partial;mode=faithful]",
        "hbt_log2[exch=partial;mode=faithful]",
    ]
    fitted = by_role[("fitted", "power3")]
    cal = json.loads(P3.read_text())
    assert fitted["args"] == [cal["estimate"]]
    assert fitted["train_quasi_loglik"] == cal["loglik"]
    assert fitted["train_events"] == cal["n_events"] == 58620
    assert fitted["source"]["trust_verdict"] == "exportable"
    assert fitted["source"]["ci_bootstrap_n"] == cal["ci_bootstrap"]  # param_name n: already on the n scale
    assert fitted["source"]["n"] == cal["estimate"]
    assert fitted["source"]["calibration_run_id"] == cal["run_id"]
    assert any("#312" in n for n in fitted["notes"])
    assert not any(n.startswith("NOT VALIDATED") for n in fitted["notes"])
    # the log families carry the C1 quasi-loglik on the same events, from the calibration's info lines
    assert by_role[("fixed", "log")]["train_quasi_loglik"] == -5790712.581696745
    assert by_role[("fixed", "log2")]["train_quasi_loglik"] == -1919225.1289633897
    assert by_role[("fixed", "log2")]["train_events"] == 58620
    assert by_role[("default", "power3")]["args"] == [2.0]
    # held-out metrics of both the fitted and the default model, so a reader can choose between them
    assert fitted["source"]["held_out"]["model_id"] == fitted["model_id"]
    assert fitted["source"]["held_out"]["brier"] == next(iter(cal["held_out"].values()))["brier"]
    base = by_role[("default", "power3")]["source"]["held_out"]
    assert base["model_id"] == by_role[("default", "power3")]["model_id"]
    assert base["fill_rate_bias"] == next(iter(cal["baseline_held_out"].values()))["fill_rate_bias"]
    # deterministic: a second export is byte-identical
    out2 = tmp_path / "cfg2.json"
    assert ec.main(["--calibration", str(P3), "--log", "--defaults", "--out", str(out2)]) == 0
    assert out.read_bytes() == out2.read_bytes()


def test_non_refusing_trust_flags_become_notes() -> None:
    """The fixture's fit pools AAPL (n in [2.175, 2.197]) and MSFT (n in [3.673, 3.686]): the entry must say so."""
    cal = load_calibration(P3)
    (fitted,) = ec.entries_for(cal, force=False, log=False, defaults=False)
    src = fitted["source"]
    assert src["trust_reasons"] == []
    assert "heterogeneous_strata" in src["trust_flags"]
    note = next(n for n in fitted["notes"] if n.startswith("note (heterogeneous_strata): "))
    assert "one pooled n does not describe both" in note
    # same text as the header of `queuetruth export hftbacktest`
    from queuetruth.export import trust

    header = [h[2:] for h in trust.header_lines(cal, "n") if h.startswith("# note (")]
    assert header == [n for n in fitted["notes"] if n.startswith("note (")]
    assert fitted["notes"][0] == f"held-out: {trust.held_out_summary(trust.assess(cal))}"
    inst = {s["stratum"]: s for s in src["stratified"] if s["stratum_kind"] == "instrument"}
    assert inst["13"]["ci_n"][1] < inst["5291"]["ci_n"][0]  # disjoint per-instrument intervals


def test_ln_n_scale_is_converted() -> None:
    cal = load_calibration(P3)
    ln = cal.model_copy(
        update={
            "param_name": "ln_n",
            "estimate": math.log(cal.estimate),
            "ci_bootstrap": [math.log(x) for x in cal.ci_bootstrap],
        }
    )
    (e,) = ec.entries_for(ln, force=False, log=False, defaults=False)
    assert e["args"][0] == pytest.approx(cal.estimate, rel=1e-12)
    assert e["source"]["estimate"] == math.log(cal.estimate)  # the calibration's own value, on its scale
    assert e["source"]["ci_bootstrap_n"] == pytest.approx(list(cal.ci_bootstrap), rel=1e-12)
    assert e["source"]["n"] == pytest.approx(cal.estimate, rel=1e-12)


def test_calibrations_on_different_bases_are_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    doc = json.loads(P1.read_text())
    doc["train"] = {"dates": None, "blocks": {"index": [0, 1, 2, 3], "n_blocks": 5}}
    other = tmp_path / "calibration.json"
    other.write_text(json.dumps(doc))
    out = tmp_path / "cfg.json"
    assert ec.main(["--calibration", str(P3), "--calibration", str(other), "--force", "--out", str(out)]) == 2
    err = capsys.readouterr().err
    assert "QT-CONFIG-001" in err and "train differ" in err
    assert not out.exists()


def test_private_helpers_are_restated_faithfully() -> None:
    from queuetruth.export import _text

    cal = load_calibration(P3)
    assert ec.LOG_N_NAMES == _text.LOG_N_NAMES
    for fam in ("hbt_power3", "power2", "databento_mc", " HBT_LOG "):
        assert ec.family_key(fam) == _text.family_key(fam)
    for c in (
        cal,
        cal.model_copy(update={"param_name": "ln_n", "estimate": 1.1}),
        cal.model_copy(update={"estimate": None}),
    ):
        assert ec.power_n(c) == _text.power_n(c)
        assert ec.spec_value(c, "exchange", "x") == _text.spec_value(c, "exchange", "x")


def test_refused_calibration_needs_force(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "cfg.json"
    assert ec.main(["--calibration", str(P1), "--out", str(out)]) == 1  # a trust refusal, as `queuetruth export`
    assert "QT-CALIB-001" in capsys.readouterr().err
    assert not out.exists()
    assert ec.main(["--calibration", str(P1), "--force", "--out", str(out)]) == 0
    (m,) = hc.load(out)["models"]
    assert m["source"]["trust_verdict"] == "refused"
    assert m["source"]["trust_reasons"] == ["not_beating_default"]
    assert m["notes"][0].startswith("NOT VALIDATED (exported with --force): not_beating_default")


def test_several_calibrations_dedupe_shared_entries(tmp_path: Path) -> None:
    doc = ec.build([str(P3), str(P1)], force=True, log=True, defaults=True)
    ids = [m["model_id"] for m in doc["models"]]
    assert len(ids) == len(set(ids))
    # log/log2 come once (from the first calibration); each power family keeps its fitted and default entries
    assert sum(1 for i in ids if i.startswith("hbt_log")) == 2
    assert {m["family"] for m in doc["models"] if m["role"] == "fitted"} == {"power3", "power"}


def test_non_power_family_and_missing_estimate_are_errors() -> None:
    cal = load_calibration(P3)
    with pytest.raises(QtError, match="QT-CONFIG-001"):
        ec.entries_for(cal.model_copy(update={"family": "databento"}), force=True, log=False, defaults=False)
    with pytest.raises(QtError, match="QT-CALIB-001"):
        ec.entries_for(cal.model_copy(update={"estimate": math.nan}), force=True, log=False, defaults=False)


def test_comparison_logliks_parses_only_the_info_lines() -> None:
    cal = load_calibration(P3)
    assert ec.comparison_logliks(cal) == {"log": (-5790712.581696745, 58620), "log2": (-1919225.1289633897, 58620)}
    cal2 = cal.model_copy(update={"warnings": ["info: comparison quasi log likelihood hbt_log = nan (3 events)"]})
    assert ec.comparison_logliks(cal2) == {}
    e = ec.entries_for(cal2, force=True, log=True, defaults=False)
    assert e[1]["train_quasi_loglik"] is None
    assert "no comparison log likelihood" in e[1]["notes"][0]


def test_missing_calibration_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert ec.main(["--calibration", str(tmp_path / "nope"), "--out", str(tmp_path / "x.json")]) == 2
    assert "QT-INPUT-001" in capsys.readouterr().err
