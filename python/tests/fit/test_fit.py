"""`queuetruth fit` and `qt.fit` end to end.

Order-level data (the committed synthetic fixture tests/fixtures/synth_small, two days, the real engine): the
NautilusTrader touch fit beats `prob_fill_on_limit = 1` on the held-out day and its snippet is written; the
hftbacktest `power3` fit does not beat `n = 2` there, so it is refused with its reasons and nothing is written; a
15-minute capture is split where the data are and refused for too little data, saying how much more is needed.

Live fills (a fake zoo runner, as in score/test_score_calibrate_live.py): every live order fills 20 s after its
ack, which a power model with `n = 0.5` reproduces (fill at `10 s / n`); the default `n = 2` fills at 5 s. The fit
recovers a value whose interval holds 0.5, beats the default on the held-out day and writes the snippet."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest
from score.qt_score_helpers import DAY, T0, S, log_from, order_events
from score.test_score_calibrate_live import PX, ZooByParam, _dataset

import queuetruth as qt
from queuetruth import cli
from queuetruth.errors import QtError
from queuetruth.fit import FitResult

REPO = Path(__file__).resolve().parents[3]
DATA = REPO / "tests" / "fixtures" / "synth_small"
#: The true power parameter of the synthetic live log: fills 10 s / N_TRUE = 20 s after the ack.
N_TRUE = 0.5


@pytest.fixture
def run(capsys: pytest.CaptureFixture[str]) -> Any:
    def _run(*argv: str) -> tuple[int, str, str]:
        code = cli.main(list(argv))
        got = capsys.readouterr()
        return code, got.out, got.err

    return _run


# ---- order-level data (real engine) ---------------------------------------------------------------------------


@pytest.mark.requires_core
def test_nautilus_fit_passes_and_writes_the_snippet(run: Any, tmp_path: Path) -> None:
    out = tmp_path / "fit"
    code, stdout, _ = run("fit", "--backtester", "nautilus", "--data", str(DATA), "--out", str(out))
    assert code == 0, stdout
    first = stdout.splitlines()[0]
    assert first.startswith("Use p = ") and "for NautilusTrader prob_fill_on_limit" in first
    assert "it beats the default p = 1" in first and "95% CI" in first and "held-out data (2026-06-16, " in first
    assert f"Snippet: {out / 'nautilus.py'}" in first
    assert "split: the first 70% of the 2 days: train 2026-06-15 (" in stdout
    assert "verdict: PASS" in stdout and "no snippet written" not in stdout
    assert "qtn.venue_options(qtn.load_calibration(" in stdout
    snippet = (out / "nautilus.py").read_text()
    assert "prob_fill_on_limit=" in snippet and "NOT VALIDATED" not in snippet
    doc = json.loads((out / "fit.json").read_text())
    assert doc["schema"] == "qt.fit" and doc["verdict"] == {"passed": True, "reasons": [], "remedies": []}
    assert doc["command"] == "calibrate nt-touch" and doc["family"] == "nt_prob_touch"
    p = doc["param"]
    assert p["name"] == "p" and p["default"] == 1.0 and p["ci"][0] <= p["value"] <= p["ci"][1] < 1.0
    assert doc["split"]["train_window"] == {"dates": ["2026-06-15"]}
    assert all(r["result"] in ("better", "tie") for r in doc["held_out"])
    assert (out / "calibration" / "calibration.json").is_file() and (out / "shadows.parquet").is_file()


@pytest.mark.requires_core
def test_qt_fit_returns_the_same_answer(tmp_path: Path) -> None:
    res = qt.fit("nautilus", data=DATA, out=tmp_path / "py", test_after="18:00")
    assert isinstance(res, FitResult) and res.passed
    assert res.summary.startswith("Use p = ") and "held-out data (from 18:00:00, " in res.summary
    assert res.snippet is not None and "prob_fill_on_limit=" in res.snippet
    assert res.doc["split"]["how"] == "held out after 18:00:00 venue time"
    assert res.calibration.trust is not None and res.calibration.trust.verdict == "exportable"
    assert res.text().splitlines()[0] == res.summary


@pytest.mark.requires_core
def test_hftbacktest_fit_that_loses_to_the_default_is_refused(run: Any, tmp_path: Path) -> None:
    out = tmp_path / "hbt"
    code, stdout, _ = run("fit", "--backtester", "hftbacktest", "--data", str(DATA), "--out", str(out))
    assert code == 1
    first = stdout.splitlines()[0]
    assert first.startswith("Do not use n = ") and "for hftbacktest power_prob_queue_model3 yet: on held-out" in first
    assert "it does not beat the default n = 2 (worse at" in first and first.endswith("no snippet written.")
    assert "verdict: DO NOT USE: not_beating_default" in stdout
    assert "what would make it pass:" in stdout and "keep the default" in stdout
    assert not (out / "hftbacktest.py").exists()
    doc = json.loads((out / "fit.json").read_text())
    assert doc["snippet"] is None and doc["files"]["snippet"] is None
    assert [r["code"] for r in doc["verdict"]["reasons"]] == ["not_beating_default"]


@pytest.mark.requires_core
def test_short_capture_is_split_where_the_data_are_and_says_how_much_more(run: Any, tmp_path: Path) -> None:
    """15 minutes of shadows on one day: the old fifths of the session leave the test window empty; the 70% split
    holds out the end of the capture, and the refusal says how much more data the interval needs."""
    qt.sample_shadows({"dataset": {"path": str(DATA)}, "n": 1000, "seed": 0, "out": str(tmp_path / "all.parquet")})
    t = pq.read_table(tmp_path / "all.parquet")
    t0 = 1781481600000000000 + (13 * 3600 + 30 * 60) * S + 3600 * S  # 14:30 venue time on 2026-06-15
    ts = pc.cast(t["ts_submit"], "int64")
    pq.write_table(t.filter(pc.and_(pc.greater_equal(ts, t0), pc.less(ts, t0 + 900 * S))), tmp_path / "short.parquet")
    base = ("fit", "--backtester", "nautilus", "--data", str(DATA), "--shadows", str(tmp_path / "short.parquet"))
    code, _, err = run(*base, "--train", "blocks:0-3/5", "--test", "blocks:4/5", "--out", str(tmp_path / "fifths"))
    assert code == 2 and "QT-DATA-001" in err and "held-out window (blocks 4/5) holds no shadows" in err
    code, stdout, _ = run(*base, "--out", str(tmp_path / "short"))
    assert code == 1
    assert "split: the first 70% of the data in time: train before 14:4" in stdout
    doc = json.loads((tmp_path / "short" / "fit.json").read_text())
    assert doc["split"]["n_train"] > 0 and doc["split"]["n_test"] > 0
    assert "degenerate_ci" in [r["code"] for r in doc["verdict"]["reasons"]]
    assert any("more minutes of data, or 5 trading days" in m for m in doc["verdict"]["remedies"])
    assert not (tmp_path / "short" / "nautilus.py").exists()


@pytest.mark.requires_core
def test_databento_fit_writes_its_constant(tmp_path: Path) -> None:
    res = qt.fit("databento", data=DATA, out=tmp_path / "db")
    if res.passed:  # the fixture's verdict, recorded either way
        assert res.snippet is not None and "QUEUE_K = " in res.snippet and "# held-out: beats" in res.snippet
    else:
        assert res.snippet is None and not (tmp_path / "db" / "databento.py").exists()
    assert res.doc["param"]["name"] == "k" and res.doc["param"]["default"] == -0.8


@pytest.mark.requires_core
def test_json_output(run: Any, tmp_path: Path) -> None:
    code, stdout, _ = run("fit", "--backtester", "nautilus", "--data", str(DATA), "--out", str(tmp_path), "--json")
    doc = json.loads(stdout)
    assert code == 0 and doc["schema"] == "qt.fit" and doc["answer"].startswith("Use p = ")


# ---- usage errors (no engine) -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--backtester", "nautilus"], "fit needs --data DATASET"),
        (["--backtester", "nautilus", "--live", "x"], "needs the market data of the same period"),
        (["--backtester", "nautilus", "--data", "d", "--family", "power3"], "not a nautilus fill model"),
        (["--backtester", "zipline", "--data", "d"], "invalid choice"),
        (["--backtester", "nautilus", "--data", "d", "--live", "l", "--shadows", "s"], "--shadows applies"),
    ],
)
def test_usage_errors(run: Any, argv: list[str], message: str) -> None:
    code, _, err = run("fit", *argv)
    assert code == 2 and "QT-USAGE-001" in err and message in err


def test_split_and_test_after_together_are_refused(tmp_path: Path) -> None:
    live, ds = _live(tmp_path, days=2, per_day=4)
    with pytest.raises(QtError, match="not both"):
        qt.fit("hftbacktest", live=live, data=ds, split="70%", test_after="10:30", out=tmp_path / "x")
    with pytest.raises(QtError, match="give both --train and --test"):
        qt.fit("hftbacktest", live=live, data=ds, train="2026-01-01", out=tmp_path / "x")


def test_help_lists_fit(run: Any) -> None:
    code, stdout, _ = run("help", "fit")
    assert code == 0 and "--test-after HH:MM" in stdout and "--split SHARE" in stdout


# ---- live fills (fake zoo runner with a known parameter) --------------------------------------------------------


def _live(tmp_path: Path, *, days: int = 3, per_day: int = 40) -> tuple[Path, Path]:
    """`per_day` orders a day, 90 s apart from 10:00 UTC; each fills 10 s / N_TRUE after its ack, except every fifth,
    which is cancelled after 15 s unfilled."""
    oe = []
    for day in range(days):
        for i in range(per_day):
            t = T0 + day * DAY + i * 90 * S
            if i % 5 == 4:
                oe.append(order_events(f"d{day}o{i}", "buy", PX, 5, t, cancel_at=t + 15 * S, cancel_sent=t + 15 * S))
            else:
                oe.append(order_events(f"d{day}o{i}", "buy", PX, 5, t, fills=[(t + round(10 * S / N_TRUE), 5)]))
    return log_from(oe, tmp_path / "live"), _dataset(tmp_path, n_days=days)


@pytest.fixture
def fake_zoo(monkeypatch: pytest.MonkeyPatch) -> None:
    """calibrate-live runs the fake zoo (a model with parameter n fills each order 10 s / n after its entry)."""
    monkeypatch.setattr(importlib.import_module("queuetruth.score.calibrate_live"), "DefaultRunner", ZooByParam)


def test_live_fit_recovers_the_known_parameter_and_passes(run: Any, tmp_path: Path, fake_zoo: None) -> None:
    live, ds = _live(tmp_path)
    out = tmp_path / "fit"
    code, stdout, _ = run(
        "fit", "--backtester", "hftbacktest", "--live", str(live), "--data", str(ds), "--out", str(out)
    )
    assert code == 0, stdout
    first = stdout.splitlines()[0]
    assert first.startswith("Use n = ") and "it beats the default n = 2; at 10s the fill rate is off by" in first
    assert "split: the first 70% of the 3 days: train 2026-01-01, 2026-01-02 (80 live orders), held out " in stdout
    doc = json.loads((out / "fit.json").read_text())
    assert doc["command"] == "calibrate-live" and doc["source"] == "live"
    lo, hi = doc["param"]["ci"]
    assert lo <= N_TRUE <= hi and lo <= doc["param"]["value"] <= hi
    ten = next(r for r in doc["held_out"] if r["horizon"] == "10s")
    assert ten["result"] == "better" and ten["fitted_bias_pp"] == 0 and ten["default_bias_pp"] == 100
    snippet = (out / "hftbacktest.py").read_text()
    assert ".power_prob_queue_model3(" in snippet and "NOT VALIDATED" not in snippet


def test_live_fit_with_too_few_orders_says_how_many_more(tmp_path: Path, fake_zoo: None) -> None:
    live, ds = _live(tmp_path, days=2, per_day=10)
    res = qt.fit("hftbacktest", live=live, data=ds, out=tmp_path / "few")
    assert not res.passed and res.snippet is None and not (tmp_path / "few" / "hftbacktest.py").exists()
    assert res.summary.startswith("Do not use n = ") and "it was fitted on too few events" in res.summary
    remedies = " ".join(res.doc["verdict"]["remedies"])
    # 10 train orders gave 8 events (two were cancelled unfilled): 30 events need about 3.8 times as many orders
    assert "the fit saw 8 informative events, at least 30 are needed" in remedies
    assert "roughly 28 more live orders in it" in remedies


def test_live_fit_with_an_empty_held_out_window_is_an_error(tmp_path: Path, fake_zoo: None) -> None:
    live, ds = _live(tmp_path, days=1, per_day=10)
    with pytest.raises(QtError) as e:
        qt.fit("hftbacktest", live=live, data=ds, test_after="23:00", out=tmp_path / "x")
    assert e.value.code == "QT-DATA-001" and "held-out window (from 23:00:00) holds no live orders" in e.value.message
    assert e.value.hint is not None and "at least one more live order" in e.value.hint
