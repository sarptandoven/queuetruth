"""Export golden text (SPEC 5.10 layouts with the fit / held-out header), notes, fitted-only Nautilus lines (review
D4, E3, E7), the latency npz (dtype, order, determinism, rejects and clock skew: review E1, E2, E9) and run_export
with the fail-safe refusal (review D1, D2)."""

from __future__ import annotations

import json
import math
import os
import subprocess
import zipfile
from pathlib import Path

import numpy as np
import pytest
from score.qt_score_helpers import T0, log_from, order_events

from queuetruth._version import __version__ as V
from queuetruth.errors import QtError
from queuetruth.export import (
    LATENCY_DTYPE,
    ExportNote,
    ExportRefused,
    LatencyRowsSkipped,
    export_snippets,
    hftbacktest_notes,
    hftbacktest_snippet,
    latency_table,
    nautilus_notes,
    nautilus_snippet,
    run_export,
    write_latency_npz,
)
from queuetruth.schemas.models import CalibrationResult

MS = 1_000_000


H1, H10 = "1000000000", "10000000000"


def held(ll1, ll10, br1=0.1, br10=0.2):
    return {"brier": {H1: br1, H10: br10}, "logloss": {H1: ll1, H10: ll10}, "fill_rate_bias": {H1: 0.0, H10: 0.01}}


#: Held-out metrics where the fitted model beats the default at 10 s and ties at 1 s.
GOOD = dict(
    held_out={"hbt_power3[n=2.3456789]": held(0.3, 0.4, 0.1, 0.15)},
    baseline_held_out={"hbt_power3[n=2]": held(0.3, 0.5, 0.1, 0.2)},
)


def cal(**kw):
    base = dict(
        run_id="0123456789abcdef",
        spec={"exchange": "partial"},
        kind="alloc",
        family="power3",
        mode="faithful",
        param_name="n",
        estimate=2.3456789,
        ci_profile=(2.1, 2.6),
        ci_bootstrap=(2.0, 2.7),
        n_events=1000,
        n_shadows=50,
        train={"dates": ["2019-12-30"]},
        test={"blocks": {"index": [3, 4], "n_blocks": 5}},
        **GOOD,
        stratified=[],
        exports={},
        data_sources=[
            {"dataset_id": "ab" * 32, "venue": "nasdaq", "source_venue": "nasdaq", "validation_status": "real_data"}
        ],
        warnings=[],
    )
    base.update(kw)
    return CalibrationResult.model_validate(base)


HELD_LINE = "# held-out: beats the default hbt_power3[n=2] out of sample: better at 10s; tie at 1s"
HBT_GOLDEN = f"""# queuetruth {V} | calibration 0123456789abcdef | power3 fitted on 2019-12-30; held-out blocks 3-4/5
# data: nasdaq (real_data, dataset abababababab)
# fit: n = 2.34568 (alloc); 1000 events from 50 shadows; bootstrap CI [2, 2.7]; profile CI [2.1, 2.6]
{HELD_LINE}
# Paste into your existing BacktestAsset chain (keep your data/tick/lot/fee settings).
from hftbacktest import BacktestAsset
asset = (
    BacktestAsset()
    # ... your existing .data(...), .tick_size(...), .lot_size(...), fee model ...
    .power_prob_queue_model3(2.34568)
    .partial_fill_exchange()
    .intp_order_latency("lat/latency.npz")
)
"""

NT_LAT = {
    k: {"p10": 1.0, "p50": v, "p90": 1.0, "p99": 1.0, "n": 3}
    for k, v in (("entry_ns", 1_500_000.4), ("response_ns", 1.0), ("cancel_ns", 700_000.0), ("amend_ns", 900_000.0))
}
NT_GOOD = dict(
    held_out={"nt_queue_l2_prob_touch[p=0.222222]": held(0.3, 0.4)},
    baseline_held_out={"nt_queue_l2_prob_touch[p=1]": held(0.35, 0.6)},
)
NT_GOLDEN = "\n".join(
    [
        f"# queuetruth {V} | calibration 0123456789abcdef | NautilusTrader 1.x and 2.x (names checked on 1.231.0 "
        "and 2.0.0rc7;",
        "# 2.x renamed FillModel to DefaultFillModel and LatencyModel to StaticLatencyModel; the import picks either)",
        "# nt_queue_l2_prob_touch fitted on 2019-12-30; held-out blocks 3-4/5; data: nasdaq (real_data, dataset "
        "abababababab)",
        "# fit: p = 0.222222 (nt_touch); 1000 events from 50 shadows; bootstrap CI [2, 2.7]; profile CI [2.1, 2.6]",
        "# held-out: beats the default nt_queue_l2_prob_touch[p=1] out of sample: better at 1s, 10s",
        "try:  # nautilus_trader 2.x",
        "    from nautilus_trader.execution import DefaultFillModel as FillModel, StaticLatencyModel as LatencyModel",
        "except ImportError:  # nautilus_trader 1.x",
        "    from nautilus_trader.backtest.models import FillModel, LatencyModel",
        "fill_model = FillModel(prob_fill_on_limit=0.222222)",
        "# not fitted: prob_slippage, random_seed (set them as in your current FillModel)",
        "latency_model = LatencyModel(base_latency_nanos=0, insert_latency_nanos=1500000, "
        "update_latency_nanos=900000, cancel_latency_nanos=700000)",
        "# queue_position=True and trade_execution=True on the venue config when the fit was queue-gated (yes)",
        "",
    ]
)


def nt_cal(**kw):
    base = dict(family="nt_queue_l2_prob_touch", param_name="p", estimate=2 / 9, kind="nt_touch", **NT_GOOD)
    base.update(kw)
    return cal(**base)


def test_hftbacktest_golden():
    assert hftbacktest_snippet(cal(exports={"latency_npz": "lat/latency.npz"})) == HBT_GOLDEN


def _code_lines(text):
    return [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]


@pytest.mark.parametrize(
    ("family", "pname", "est", "exch", "line"),
    [
        ("power", "n", 2.0, "partial", "    .power_prob_queue_model(2)"),
        ("power2", "ln_n", 0.0, "no_partial", "    .power_prob_queue_model2(1)"),
        ("databento", "k", -0.8, "partial", "    .power_prob_queue_model3(5)"),
        ("log", None, None, "partial", "    .log_prob_queue_model()"),
        ("log2", None, None, "partial", "    .log_prob_queue_model2()"),
        ("risk_adverse", None, None, "partial", "    .risk_adverse_queue_model()"),
        ("hbt_power3", "n", 3.0, "partial", "    .power_prob_queue_model3(3)"),
    ],
)
def test_hftbacktest_builders(family, pname, est, exch, line):
    text = hftbacktest_snippet(cal(family=family, param_name=pname, estimate=est, spec={"exchange": exch}))
    lines = _code_lines(text)
    assert lines[:3] == ["from hftbacktest import BacktestAsset", "asset = (", "    BacktestAsset()"]
    assert lines[3] == line
    assert lines[4] == ("    .no_partial_fill_exchange()" if exch == "no_partial" else "    .partial_fill_exchange()")
    assert lines[5] == ")" and "intp_order_latency" not in text
    compile(text, "snippet", "exec")


@pytest.mark.parametrize(
    ("family", "est"), [("databento", 0.3), ("nt_prob_touch", 0.2), ("databento", None), ("databento", float("nan"))]
)
def test_hftbacktest_builder_omitted_with_note(family, est):
    c = cal(family=family, param_name="k", estimate=est)
    text = hftbacktest_snippet(c)
    assert "_queue_model" not in text and "    .partial_fill_exchange()" in text
    assert "nan" not in text.lower().replace("nautilus", "")
    notes = hftbacktest_notes(c)
    assert any("omitted" in n for n in notes)
    assert any("#316" in n and "#312" in n for n in notes) and any("conflated L2" in n for n in notes)


def test_l2_note_states_the_view_the_fit_used():
    """Trader review D5: the note said "conflated L2 depth" for a fit run at cadence 0."""
    notes = hftbacktest_notes(cal(spec={"exchange": "partial", "l2": {"cadence_ns": 0, "depth_levels": 10}}))
    assert any("top 10 levels, unconflated (every book event)" in n and "conflated L2" in n for n in notes)
    notes = hftbacktest_notes(cal(spec={"l2": {"cadence_ns": 100_000_000, "depth_levels": 0}}))
    assert any("all levels, conflated every 100000000 ns" in n for n in notes)


def test_databento_note_expected_value():
    assert any("expected-value approximation" in n for n in hftbacktest_notes(cal(family="databento", estimate=-0.5)))


def test_nautilus_golden():
    c = nt_cal(latency={**NT_LAT, "clock_sync_assumed": True})
    assert nautilus_snippet(c) == NT_GOLDEN
    assert any("per-touch" in n for n in nautilus_notes(c))
    compile(nautilus_snippet(c), "nt", "exec")


#: The constructors of each NautilusTrader release line, by module (keywords checked on 1.231.0 and 2.0.0rc7). The
#: 2.x `nautilus_trader.execution` has no `FillModel`; 1.x has no `DefaultFillModel` and 2.x no `backtest.models`.
NT_LAYOUTS = {
    1: {
        "nautilus_trader.backtest.models": {"FillModel": "fill", "LatencyModel": "latency"},
        "nautilus_trader.execution": {},
    },
    2: {"nautilus_trader.execution": {"DefaultFillModel": "fill", "StaticLatencyModel": "latency"}},
}
NT_KWARGS = {
    "fill": {"prob_fill_on_limit", "prob_slippage", "random_seed"},
    "latency": {"base_latency_nanos", "insert_latency_nanos", "update_latency_nanos", "cancel_latency_nanos"},
}


def _fake_nautilus(monkeypatch, major):
    """Stand-in `nautilus_trader` modules of release line `major` whose constructors reject unknown keywords."""
    import sys
    import types

    def model(kind, name):
        def __init__(self, **kw):
            bad = set(kw) - NT_KWARGS[kind]
            if bad:
                raise TypeError(f"{name}() got an unexpected keyword argument {sorted(bad)[0]!r}")
            self.kind, self.kw = kind, kw

        return type(name, (), {"__init__": __init__})

    for mod in [k for k in sys.modules if k == "nautilus_trader" or k.startswith("nautilus_trader.")]:
        monkeypatch.delitem(sys.modules, mod)
    monkeypatch.setitem(sys.modules, "nautilus_trader", types.ModuleType("nautilus_trader"))
    for path, classes in NT_LAYOUTS[major].items():
        m = types.ModuleType(path)
        for name, kind in classes.items():
            setattr(m, name, model(kind, name))
        monkeypatch.setitem(sys.modules, path, m)


@pytest.mark.parametrize("major", [1, 2])
def test_nautilus_snippet_runs_on_both_release_lines(monkeypatch, major):
    """The nautilus-plugin finding: 0.1.1 wrote `delete_latency_nanos` and imported `nautilus_trader.backtest.models`,
    which 2.x lacks. The snippet now binds the release line's own classes and keywords."""
    _fake_nautilus(monkeypatch, major)
    ns: dict = {}
    exec(nautilus_snippet(nt_cal(latency={**NT_LAT, "clock_sync_assumed": True})), ns)
    assert ns["fill_model"].kw == {"prob_fill_on_limit": 0.222222}
    assert ns["latency_model"].kw == {
        "base_latency_nanos": 0,
        "insert_latency_nanos": 1500000,
        "update_latency_nanos": 900000,
        "cancel_latency_nanos": 700000,
    }
    assert type(ns["fill_model"]).__name__ == ("DefaultFillModel" if major == 2 else "FillModel")
    # Fill model only: the import names one class per line.
    ns = {}
    exec(nautilus_snippet(nt_cal()), ns)
    assert ns["fill_model"].kind == "fill" and "latency_model" not in ns and "LatencyModel" not in ns


#: Interpreters with nautilus_trader installed: QT_NT_PYTHON (several joined with os.pathsep, e.g. a 1.x and a 2.x
#: venv), as for the cross-check in tests/nt_crosscheck.
NT_PYTHONS = [p for p in os.environ.get("QT_NT_PYTHON", "").split(os.pathsep) if p and Path(p).exists()]


@pytest.mark.parametrize("py", NT_PYTHONS or [None])
def test_nautilus_snippet_runs_on_installed_nautilus_trader(py, tmp_path):
    """Run the snippet in a real nautilus_trader (checked with 1.231.0 and 2.0.0rc7 venvs)."""
    if py is None:
        pytest.skip("QT_NT_PYTHON names no interpreter with nautilus_trader")
    script = tmp_path / "snippet.py"
    script.write_text(
        nautilus_snippet(nt_cal(latency={**NT_LAT, "clock_sync_assumed": True}))
        + "print(type(fill_model).__name__, type(latency_model).__name__)\n"
    )
    out = subprocess.run([str(py), "-I", str(script)], capture_output=True, text=True, timeout=120, cwd=tmp_path)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() in (["FillModel", "LatencyModel"], ["DefaultFillModel", "StaticLatencyModel"])


def test_nautilus_emits_no_unfitted_lines_for_non_nt_family():
    """Trader review D4: a power-family fit must not overwrite the user's FillModel or LatencyModel."""
    text = nautilus_snippet(cal())
    code = _code_lines(text)
    assert code == [], code
    assert "# fill_model = FillModel(prob_fill_on_limit=1)  # NautilusTrader default, NOT fitted" in text
    assert "# latency_model: not fitted (no latency calibration in this result)" in text
    assert "(no)" in text
    notes = nautilus_notes(cal())
    assert any("prob_fill_on_limit not fitted" in n for n in notes)
    assert any("latency not fitted" in n for n in notes)
    compile(text, "nt", "exec")


def test_nautilus_latency_without_exchange_timestamps_is_not_emitted():
    """Python review E3: acks stamped record_time give n = 0 latency samples; no all-zero LatencyModel."""
    zero = {k: {"p10": None, "p50": None, "p90": None, "p99": None, "n": 0} for k in NT_LAT}
    c = nt_cal(latency={**zero, "clock_sync_assumed": True})
    text = nautilus_snippet(c)
    assert "LatencyModel(" not in "\n".join(_code_lines(text))
    assert "record_time" in text and "insert_latency_nanos=0" not in text
    # Partial: entry and cancel fitted, amend not.
    part = {**NT_LAT, "amend_ns": {"p10": None, "p50": None, "p90": None, "p99": None, "n": 0}}
    text = nautilus_snippet(nt_cal(latency={**part, "clock_sync_assumed": True}))
    assert (
        "latency_model = LatencyModel(base_latency_nanos=0, insert_latency_nanos=1500000, "
        "cancel_latency_nanos=700000)" in text
    )
    assert "# not fitted (no samples): update_latency_nanos" in text


def test_nautilus_touch_family_without_estimate_emits_no_fill_model():
    """Python review E7: no estimate must not become prob_fill_on_limit=1."""
    text = nautilus_snippet(nt_cal(estimate=None))
    assert not any("FillModel(" in ln for ln in _code_lines(text))
    assert "nt_queue_l2_prob_touch has no finite estimate" in text


def test_export_snippets_fill_exports():
    ex = export_snippets(cal(), latency_npz="x.npz")
    assert ex.latency_npz == "x.npz" and '.intp_order_latency("x.npz")' in ex.hftbacktest.python
    assert ex.nautilus.python == nautilus_snippet(cal())
    assert any("relative to the calibration" in n for n in ex.hftbacktest.notes)


def _live(tmp_path):
    oe = [
        order_events("b", "buy", 1, 1, T0 + 5 * MS),
        order_events("a", "buy", 1, 1, T0 + 2 * MS),
        order_events("c", "buy", 1, 1, T0 + 9 * MS),
    ]
    _o, ev = oe[2]
    ev[1].update(event_type="reject", ts_exchange=None)
    oe.append(order_events("d", "buy", 1, 1, T0 + 20 * MS))
    oe[3][1][1]["ts_exchange"] = None  # ack without exchange time: skipped
    return log_from(oe, tmp_path / "live")


def test_latency_npz_dtype_order_and_determinism(tmp_path):
    live = _live(tmp_path)
    with pytest.warns(LatencyRowsSkipped, match="no_exchange_time 1, reject_without_exchange_time 1"):
        fe = write_latency_npz(str(live), str(tmp_path / "l1.npz"))
        write_latency_npz(str(live), str(tmp_path / "l2.npz"))
    assert (tmp_path / "l1.npz").read_bytes() == (tmp_path / "l2.npz").read_bytes()
    assert fe.rows == 2 and fe.name == "l1.npz"
    with zipfile.ZipFile(tmp_path / "l1.npz") as z:
        assert z.namelist() == ["data.npy"]
        assert z.getinfo("data.npy").compress_type == zipfile.ZIP_DEFLATED
    arr = np.load(tmp_path / "l1.npz")["data"]
    assert arr.dtype == LATENCY_DTYPE and arr.dtype.itemsize == 32
    assert arr["req_ts"].tolist() == [T0 + MS, T0 + 4 * MS]
    assert arr["exch_ts"].tolist() == [T0 + 2 * MS, T0 + 5 * MS]
    assert arr["resp_ts"].tolist() == [T0 + 3 * MS, T0 + 6 * MS]
    assert arr["_padding"].tolist() == [0, 0]


def _po_log(tmp_path, kinds, *, skew=None):
    """Orders 1 s apart; `kinds[i]` is ack | po_rej (post-only reject WITH an exchange time) | rej_noexch;
    `skew[i]` = (entry, response) latency override."""
    oe = []
    for i, kind in enumerate(kinds):
        t = T0 + i * 1_000_000_000
        o, ev = order_events(f"o{i}", "buy", 1, 1, t + 300_000)
        ev[0]["ts_local_send"] = t
        ent, resp = (skew or {}).get(i, (300_000, 200_000))
        ev[1].update(ts_exchange=t + ent, ts_local_recv=t + ent + resp)
        if kind == "po_rej":
            ev[1]["event_type"] = "post_only_reject"
        elif kind == "rej_noexch":
            ev[1].update(event_type="reject", ts_exchange=None)
        oe.append((o, ev))
    return log_from(oe, tmp_path / "live")


def _intp_entry(arr, ts):
    """hbt IntpOrderLatency.entry (hftbacktest 0.9.4 latency.rs, transliterated by the Python review)."""

    def intp(x, x1, y1, x2, y2):
        return int(float(y2 - y1) / float(x2 - x1) * float(x - x1)) + y1

    if ts < arr[0]["req_ts"]:
        return int(arr[0]["exch_ts"] - arr[0]["req_ts"])
    for i in range(len(arr)):
        if i + 1 >= len(arr):
            return int(arr[-1]["exch_ts"] - arr[-1]["req_ts"])
        r, n = arr[i], arr[i + 1]
        if r["req_ts"] <= ts < n["req_ts"]:
            if r["exch_ts"] <= 0 or n["exch_ts"] <= 0:
                return -intp(
                    ts,
                    int(r["req_ts"]),
                    int(r["resp_ts"] - r["req_ts"]),
                    int(n["req_ts"]),
                    int(n["resp_ts"] - n["req_ts"]),
                )
            return intp(
                ts, int(r["req_ts"]), int(r["exch_ts"] - r["req_ts"]), int(n["req_ts"]), int(n["exch_ts"] - n["req_ts"])
            )
    raise AssertionError


def test_latency_npz_post_only_reject_keeps_exchange_time(tmp_path):
    """Python review E1 (ported from the review's `lat_exp.py`): a post-only reject used to be written with
    exch_ts = 0, so hbt interpolated NEGATIVE entry latencies (rejecting simulated orders) between t0+1 s and t0+3 s.
    A reject that carries an exchange time reached the engine and keeps it; one without is dropped."""
    live = _po_log(tmp_path, ["ack", "ack", "po_rej", "ack", "ack"])
    write_latency_npz(str(live), str(tmp_path / "po.npz"))
    arr = np.load(tmp_path / "po.npz")["data"]
    assert (arr["exch_ts"] > 0).all() and len(arr) == 5
    for k in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5):
        assert _intp_entry(arr, T0 + int(k * 1e9)) == 300_000
    live2 = _po_log(tmp_path / "b", ["ack", "rej_noexch", "ack"])
    with pytest.warns(LatencyRowsSkipped, match="reject_without_exchange_time 1"):
        fe = write_latency_npz(str(live2), str(tmp_path / "b.npz"))
    assert fe.rows == 2


def test_latency_npz_drops_negative_latencies(tmp_path):
    """Python review E2: clock-skewed rows (exch < req, resp < exch) are dropped and counted, as latency_stats does
    (hbt rejects orders around a negative entry latency and asserts lat >= 0 on responses)."""
    live = _po_log(tmp_path, ["ack", "ack", "ack", "ack"], skew={1: (-1_000_000, 200_000), 2: (300_000, -500)})
    arr, skipped = latency_table(live)
    assert skipped["negative_latency"] == 2 and len(arr) == 2
    assert ((arr["exch_ts"] >= arr["req_ts"]) & (arr["resp_ts"] >= arr["exch_ts"])).all()


def test_latency_npz_appends_suffix(tmp_path):
    """Python review E9: hbt picks its loader from the extension; like np.savez_compressed, `.npz` is appended."""
    live = _po_log(tmp_path, ["ack", "ack"])
    fe = write_latency_npz(str(live), str(tmp_path / "latency"))
    assert fe.name == "latency.npz" and (tmp_path / "latency.npz").exists() and not (tmp_path / "latency").exists()


def test_latency_npz_requires_local_times(tmp_path):
    o, ev = order_events("a", "buy", 1, 1, T0)
    ev[0]["ts_local_send"] = None
    live = log_from([(o, ev)], tmp_path / "live")
    with pytest.raises(QtError) as e:
        write_latency_npz(str(live), str(tmp_path / "x.npz"))
    assert e.value.code == "QT-INPUT-002"


def test_run_export_targets(tmp_path):
    p = tmp_path / "calibration.json"
    p.write_text(json.dumps(cal().model_dump(mode="json")))
    with pytest.warns(ExportNote) as notes:
        s = run_export({"target": "hftbacktest", "calibration": {"path": str(tmp_path)}, "out": str(tmp_path / "h.py")})
    assert s.text == hftbacktest_snippet(cal()) and (tmp_path / "h.py").read_text() == s.text
    msgs = [str(w.message) for w in notes]
    assert msgs == hftbacktest_notes(cal()) and any("#312" in m and "#316" in m for m in msgs)
    assert s.files[0].name == "h.py"
    with pytest.warns(ExportNote):
        n = run_export({"target": "nautilus", "calibration": {"path": str(p)}})
    assert n.text == nautilus_snippet(cal()) and n.files == []
    live = _po_log(tmp_path, ["ack", "ack", "ack"])
    lz = run_export({"target": "latency_npz", "live": {"path": str(live)}, "out": str(tmp_path / "lat.npz")})
    assert lz.files[0].rows == 3 and lz.text is None
    with pytest.raises(QtError):
        run_export({"target": "l2_npz", "out": "x"})


#: Review D1 reproduction: fitted on a date without data (n_events 0), estimate at the grid minimum, zero-width CI.
D1 = dict(
    estimate=math.exp(-3),
    n_events=0,
    n_shadows=0,
    train={"dates": ["2026-06-17"]},
    test={"dates": ["2026-06-16"]},
    ci_bootstrap=(math.exp(-3), math.exp(-3)),
    ci_profile=None,
    held_out={},
    baseline_held_out={},
    spec={"exchange": "partial", "grid": {"alloc": {"lo": -3.0, "hi": 4.0, "n": 141}}},
)
#: Review D2 reproduction: n = 0.986 loses to the default n = 2 on held-out log loss at 10 s and 300 s.
D2 = dict(
    estimate=0.986,
    held_out={
        "hbt_power3[n=0.986]": {
            "brier": {"10000000000": 0.004, "300000000000": 0.03},
            "logloss": {"10000000000": 0.0277, "300000000000": 0.1022},
            "fill_rate_bias": {},
        }
    },
    baseline_held_out={
        "hbt_power3[n=2]": {
            "brier": {"10000000000": 0.003, "300000000000": 0.025},
            "logloss": {"10000000000": 0.0139, "300000000000": 0.0876},
            "fill_rate_bias": {},
        }
    },
)


@pytest.mark.parametrize("case", ["d1", "d2"])
@pytest.mark.parametrize("target", ["hftbacktest", "nautilus"])
def test_run_export_refuses_untrusted_calibration_unless_forced(case, target, tmp_path):
    c = cal(**(D1 if case == "d1" else D2))
    (tmp_path / "calibration.json").write_text(json.dumps(c.model_dump(mode="json")))
    out = tmp_path / "snippet.py"
    spec = {"target": target, "calibration": {"path": str(tmp_path)}, "out": str(out)}
    with pytest.raises(ExportRefused) as e:
        run_export(spec)
    assert e.value.code == "QT-CALIB-001" and e.value.exit_code == 1 and not out.exists()
    if case == "d1":
        assert "few_events" in str(e.value) and "at_grid_edge" in str(e.value) and "degenerate_ci" in str(e.value)
        assert "no_held_out" in str(e.value)
    else:
        assert "not_beating_default" in str(e.value) and "WORSE at 10s, 5m" in str(e.value)
    assert "--force" in (e.value.hint or "")
    with pytest.warns(ExportNote, match="--force: writing a refused calibration"):
        s = run_export(spec, force=True)
    text = out.read_text()
    assert s.text == text
    assert "# !! NOT VALIDATED: `queuetruth export` refuses this calibration" in text
    if case == "d2":
        assert "# held-out: DOES NOT BEAT the default hbt_power3[n=2] out of sample: WORSE at 10s, 5m" in text
        assert "(log loss at 10s: fitted 0.0277 vs default 0.0139" in text
    compile(text, "snippet", "exec")


def test_snippet_header_states_window_events_and_held_out():
    for text in (hftbacktest_snippet(cal()), nautilus_snippet(cal())):
        assert "fitted on 2019-12-30; held-out blocks 3-4/5" in text
        assert "1000 events from 50 shadows" in text
        assert HELD_LINE in text
        assert "NOT VALIDATED" not in text


def _hbt_python(repo_root: Path) -> Path | None:
    env = os.environ.get("QT_HBT_PYTHON")
    if env:
        return Path(env)
    for name in ("hbt-venv", "w3trust-hbt"):
        p = repo_root / ".scratch" / name / "bin" / "python"
        if p.exists():
            return p
    return None


@pytest.mark.parametrize("family", ["power", "power2", "power3", "databento", "log", "log2", "risk_adverse"])
def test_hftbacktest_snippet_runs_in_hftbacktest(family, tmp_path, repo_root):
    """Executes the snippet in hftbacktest 2.4.4 (separate venv: `make hbt-crosscheck` or QT_HBT_PYTHON) and runs a
    tiny backtest with it, including the generated IntpOrderLatency npz."""
    py = _hbt_python(repo_root)
    if py is None:
        pytest.skip("hftbacktest venv missing (run `make hbt-crosscheck` or set QT_HBT_PYTHON)")
    live = _live(tmp_path)
    npz = tmp_path / "lat.npz"
    write_latency_npz(str(live), str(npz))
    est = {"power": 2.0, "power2": 1.5, "power3": 3.0, "databento": -0.8}.get(family)
    c = cal(family=family, estimate=est, exports={"latency_npz": str(npz) if family == "power3" else None})
    snip = tmp_path / "snippet.py"
    snip.write_text(hftbacktest_snippet(c))
    check = Path(__file__).with_name("hbt_load_check.py")
    r = subprocess.run(
        [str(py), "-I", str(check), str(snip), str(tmp_path)], capture_output=True, text=True, timeout=600
    )
    assert r.returncode == 0, r.stderr[-2000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["ok"] is True
