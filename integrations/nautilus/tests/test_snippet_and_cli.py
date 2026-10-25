"""Rendered code and the `queuetruth-nautilus` command (rendering needs no nautilus_trader)."""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys

import pytest
from conftest import FIXTURES, variant, write

import queuetruth_nautilus as qtn
from queuetruth_nautilus.cli import main

HAVE_NT = importlib.util.find_spec("nautilus_trader") is not None


@pytest.mark.parametrize("major", [1, 2])
def test_render_names_per_major(touch_doc, major):
    text = qtn.render_snippet(qtn.from_document(touch_doc), major, random_seed=3)
    ast.parse(text)
    if major == 2:
        assert "from nautilus_trader.execution import DefaultFillModel" in text
        assert "from nautilus_trader.model import BookType" in text
    else:
        assert "from nautilus_trader.backtest.models import FillModel" in text
        assert "from nautilus_trader.model.enums import BookType" in text
    assert "prob_fill_on_limit=0.28662597114317423, prob_slippage=0.0, random_seed=3)" in text
    assert "queue_position=False," in text
    assert "latency_model = " not in text and "# latency_model: not fitted; keep your own" in text


@pytest.mark.parametrize("major", [1, 2])
def test_render_latency_uses_cancel_keyword(latency_doc, major):
    text = qtn.render_snippet(qtn.from_document(latency_doc), major)
    ast.parse(text)
    assert "cancel_latency_nanos=180000" in text and "delete_latency_nanos" not in text
    assert ("StaticLatencyModel(" in text) == (major == 2)
    # only fitted values become code: amend has no samples, so it stays at the class default (0) with a comment
    assert "(base_latency_nanos=0, insert_latency_nanos=250400, cancel_latency_nanos=180000)" in text
    assert "update_latency_nanos=" not in text
    assert "# not fitted (no samples): update_latency_nanos; 0 ns by default, set your own" in text


def _warned(fn, *args, **kwargs) -> tuple[object, list[str]]:
    import warnings

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        out = fn(*args, **kwargs)
    return out, [str(x.message) for x in w if issubclass(x.category, qtn.CalibrationWarning)]


def test_render_caveats_for_the_requested_major(touch_doc, queue_doc):
    """`render_snippet` gives the same CalibrationWarnings as the constructors, for the release line it renders."""
    _, w = _warned(qtn.render_snippet, qtn.from_document(variant(touch_doc, mode="corrected")), 1, random_seed=1)
    assert any("'corrected' mode" in m for m in w)
    gated = qtn.from_document(variant(queue_doc, trust={"verdict": "exportable"}))
    _, w2 = _warned(qtn.render_snippet, gated, 2, random_seed=1)
    _, w1 = _warned(qtn.render_snippet, gated, 1, random_seed=1)
    assert any("1.231.0 queue rules" in m for m in w2)
    assert not any("1.231.0 queue rules" in m for m in w1)


def test_render_seed(touch_doc):
    cal = qtn.from_document(touch_doc)
    text, w = _warned(qtn.render_snippet, cal, 2)
    assert "# random_seed not set: each at-price print is a random draw" in text
    assert any("random_seed not set" in m for m in w)
    text, w = _warned(qtn.render_snippet, cal, 2, random_seed=7)
    assert "random_seed not set" not in text and not w
    assert "random_seed=7)" in text


def test_render_fee_model_note(touch_doc):
    cal = qtn.from_document(touch_doc)
    two = qtn.render_snippet(cal, 2, random_seed=1)
    one = qtn.render_snippet(cal, 1, random_seed=1)
    assert "fee_model=..., **venue_options)" in two and "FixedFeeModel(Money(0, USD))" in two
    assert "fee_model" not in one


def test_render_from_snippet_names_mode(touch_doc):
    text = (FIXTURES / "cal_touch.export.txt").read_text()
    cal = qtn.from_snippet(text)
    _, w = _warned(qtn.render_snippet, cal, 2, random_seed=1)
    assert not any("mode" in m for m in w)
    cal = qtn.from_snippet("\n".join(ln for ln in text.splitlines() if not ln.startswith("# held-out:")))
    _, w = _warned(qtn.render_snippet, cal, 2, random_seed=1)
    assert any("does not name the fit's mode" in m for m in w)


def test_render_refused_and_forced(queue_doc):
    cal = qtn.from_document(queue_doc)
    with pytest.raises(qtn.CalibrationRefused):
        qtn.render_snippet(cal, 2)
    with pytest.warns(qtn.CalibrationWarning):
        text = qtn.render_snippet(cal, 2, force=True)
    assert "# !! NOT VALIDATED (not_beating_default)" in text
    assert "queue_position=True," in text


def test_render_bad_major_and_nothing_usable(touch_doc):
    with pytest.raises(qtn.QtNautilusError, match="nt_major"):
        qtn.render_snippet(qtn.from_document(touch_doc), 3)
    with pytest.raises(qtn.QtNautilusError, match="nothing NautilusTrader can use"):
        qtn.render_snippet(qtn.from_document(variant(touch_doc, family="databento", estimate=-0.6)), 2)


def test_cli_snippet(capsys):
    assert main(["snippet", str(FIXTURES / "cal_touch.json"), "--nt-major", "2", "--seed", "1"]) == 0
    out = capsys.readouterr().out
    assert "DefaultFillModel(prob_fill_on_limit=0.28662597114317423, prob_slippage=0.0, random_seed=1)" in out


def test_cli_refused_exit_1(capsys):
    assert main(["snippet", str(FIXTURES / "cal_queue_refused.json"), "--nt-major", "1"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: QT-CALIB-001: ")
    assert "hint: " in err


def test_cli_forced_warns(capsys):
    assert main(["snippet", str(FIXTURES / "cal_queue_refused.json"), "--nt-major", "1", "--force"]) == 0
    cap = capsys.readouterr()
    assert "FillModel(prob_fill_on_limit=0.8711256117455138" in cap.out
    assert "warning: force: using a NOT VALIDATED calibration" in cap.err


@pytest.mark.parametrize("flag", [["--json"], ["--format", "json"]])
def test_cli_json_error(tmp_path, capsys, flag):
    """As `queuetruth`: with --format json (alias --json) an error is exactly one qt.error document on stdout."""
    assert main(["snippet", str(tmp_path / "missing.json"), "--nt-major", "2", *flag]) == 2
    cap = capsys.readouterr()
    doc = json.loads(cap.out)
    assert doc["schema"] == "qt.error" and doc["code"] == "QT-INPUT-001"
    assert cap.err == ""


@pytest.mark.parametrize(
    "changes",
    [
        {"ci_bootstrap": 0.3},
        {"ci_bootstrap": [0.1]},
        {"n_events": "abc"},
        {"trust": {"verdict": "exportable", "reasons": 5}},
        {"trust": {"verdict": "exportable", "beats_default": {"1s": "better"}}},
        {"trust": {"verdict": "maybe"}},
    ],
)
def test_cli_malformed_calibration_exits_2(tmp_path, touch_doc, capsys, changes):
    """A corrupt file is QT-INPUT-002 with exit 2, never a traceback with exit 1 (which means refused)."""
    p = write(tmp_path, variant(touch_doc, **changes))
    assert main(["inspect", str(p)]) == 2
    err = capsys.readouterr().err
    assert err.startswith("error: QT-INPUT-002: ") and "Traceback" not in err


def test_cli_unexpected_exception_is_engine_error(monkeypatch, capsys):
    import queuetruth_nautilus.cli as cli

    def boom(path):
        raise ZeroDivisionError("bad")

    monkeypatch.setattr(cli, "load_calibration", boom)
    assert main(["inspect", "x.json", "--format", "json"]) == 2
    doc = json.loads(capsys.readouterr().out)
    assert doc["code"] == "QT-ENGINE-001" and "ZeroDivisionError" in doc["message"]


def test_cli_snippet_without_nautilus_hints_nt_major(monkeypatch, capsys):
    from queuetruth_nautilus import compat

    def missing():
        raise qtn.QtNautilusError("QT-USAGE-001", "nautilus_trader is not installed", "pip install nautilus_trader")

    monkeypatch.setattr(compat, "installed_version", missing)
    assert main(["snippet", str(FIXTURES / "cal_touch.json")]) == 2
    err = capsys.readouterr().err
    assert "QT-USAGE-001" in err and "or pass --nt-major 1|2" in err


def test_cli_inspect_text(capsys):
    assert main(["inspect", str(FIXTURES / "cal_touch.json")]) == 0
    out = capsys.readouterr().out
    assert "prob_fill_on_limit: 0.28662597114317423" in out
    assert "1s: +0.41 pp vs +2.80 pp; Brier 0.01145 vs 0.028" in out
    assert "trust: exportable [structured]" in out
    if HAVE_NT:
        assert "built fill_model=" in out
    else:
        assert "nautilus_trader: not installed" in out


def test_cli_inspect_refused_prints_then_exits_1(capsys):
    assert main(["inspect", str(FIXTURES / "cal_queue_refused.json")]) == 1
    cap = capsys.readouterr()
    assert "trust: refused (not_beating_default)" in cap.out
    assert "QT-CALIB-001" in cap.err


def check_schema(doc: dict) -> None:
    """Validates against the schema the package ships (`queuetruth_nautilus/schemas/`)."""
    from importlib.resources import files

    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((files("queuetruth_nautilus") / "schemas" / "qt.nautilus_inspect.schema.json").read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(doc)


def test_cli_inspect_json(tmp_path, touch_doc, capsys):
    p = write(tmp_path, touch_doc)
    assert main(["inspect", str(p), "--format", "json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["schema"] == "qt.nautilus_inspect"
    assert doc["calibration"]["prob_fill_on_limit"] == 0.28662597114317423
    assert doc["refused"] is None
    assert (doc["venue_options"] is None) == (not HAVE_NT)
    assert "queue_position=True" in doc["note"]
    check_schema(doc)


@pytest.mark.parametrize("fixture", ["cal_touch_latency.json", "cal_queue_refused.json", "cal_touch.export.txt"])
def test_cli_inspect_json_matches_schema(fixture, capsys):
    main(["inspect", str(FIXTURES / fixture), "--json"])
    check_schema(json.loads(capsys.readouterr().out))


@pytest.mark.skipif(not HAVE_NT, reason="nautilus_trader not installed")
def test_cli_env(capsys):
    assert main(["env", "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["verified_line"] is True
    assert doc["fill_model"] in (
        "nautilus_trader.execution:DefaultFillModel",
        "nautilus_trader.backtest.models:FillModel",
    )


@pytest.mark.skipif(HAVE_NT, reason="nautilus_trader installed")
def test_cli_env_without_nautilus(capsys):
    assert main(["env"]) == 2
    assert "QT-USAGE-001: nautilus_trader is not installed" in capsys.readouterr().err


def test_python_m_entry_point():
    r = subprocess.run(
        [sys.executable, "-m", "queuetruth_nautilus", "snippet", str(FIXTURES / "cal_touch.json"), "--nt-major", "2"],
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "PYTHONPATH": str(FIXTURES.parents[1] / "src")},
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert "DefaultFillModel" in r.stdout


def test_cli_version(capsys):
    with pytest.raises(SystemExit) as ei:
        main(["--version"])
    assert ei.value.code == 0
    assert qtn.__version__ in capsys.readouterr().out


def test_cli_inspect_json_refused(capsys):
    assert main(["inspect", str(FIXTURES / "cal_queue_refused.json"), "--json"]) == 1
    cap = capsys.readouterr()
    doc = json.loads(cap.out)
    assert doc["refused"]["code"] == "QT-CALIB-001"
    assert doc["refused"]["details"]["reasons"] == ["not_beating_default"]
    assert doc["venue_options"] is None
    assert doc["nautilus_trader"] is None or doc["nautilus_trader"][0].isdigit()
    assert "QT-CALIB-001" not in cap.err  # one document on stdout carries the error
