"""Answer-first terminal output of the console script (trader review items 3, 5, 6-CLI, 8-CLI): the zoo leaderboard,
the scorecard headline, the calibration verdict, never success for nothing (zero-matched scorecards and empty fits
exit 2; "gates passed" only with gates set), new commands (`live import --venue sim-*`, `export replay-schedule`,
`export --force`) and the extra-missing messages. Views are unit-tested on synthetic summaries; the end-to-end
runs on `tests/fixtures/synth_small` are `integration` tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from queuetruth import cli
from queuetruth.errors import QtError

H = ("100000000", "1000000000", "10000000000", "60000000000", "300000000000")


def _ci(v: float | None, lo: float | None = None, hi: float | None = None) -> dict[str, Any]:
    return {"value": v, "ci_lo": v if lo is None else lo, "ci_hi": v if hi is None else hi}


def _entry(model: str, bias60: dict[str, Any]) -> dict[str, Any]:
    fr = {h: _ci(0.0) for h in H}
    fr["60000000000"] = bias60
    return {
        "model_id": model,
        "brier": dict.fromkeys(H, 0.0),
        "logloss": dict.fromkeys(H, 0.0),
        "fill_rate_bias": fr,
        "overfill_bias": _ci(0.0),
        "qty_bias": 0.0,
        "ttf_iad": 0.0,
        "mcnemar": {"b": 0, "c": 0, "p_exact": 1.0},
    }


def _zoo_doc() -> dict[str, Any]:
    return {
        "schema": "qt.zoo_summary",
        "schema_version": 1,
        "run_id": "0123456789abcdef",
        "spec": {"h_max_ns": 300_000_000_000},
        "n_shadows": 200,
        "models": ["hbt_power3[n=2]", "front_of_queue[mode=faithful]", "hbt_log"],
        "leaderboard": [
            _entry("hbt_power3[n=2]", _ci(-0.007, -0.013, -0.003)),
            _entry("front_of_queue[mode=faithful]", _ci(0.133, 0.101, 0.165)),
            _entry("hbt_log", _ci(0.001, -0.002, 0.004)),
        ],
        "files": [{"name": "zoo_predictions.parquet", "rows": 10, "bytes": 1, "sha256": "0" * 64}],
        "warnings": [],
    }


def test_zoo_view_prints_the_leaderboard_sorted_by_bias_at_60s() -> None:
    """Ranked and worded by `queuetruth.eval.leaderboard` (the report's single source): smallest |bias| first."""
    from queuetruth.eval.leaderboard import bias_sentence, rank_leaderboard

    doc = _zoo_doc()
    out = cli.render(["zoo"], doc)
    lines = out.splitlines()
    assert lines[0].startswith("zoo: 200 shadows, 3 models")
    assert "sorted by |fill-rate bias| at 60s, smallest first" in out
    table = [ln for ln in lines if ln.startswith(("front_of_queue", "hbt_power3", "hbt_log"))]
    ranked = [e["model_id"] for e in rank_leaderboard(doc["leaderboard"], 60 * 10**9)]
    assert [ln.split()[0] for ln in table] == ranked == ["hbt_log", "hbt_power3[n=2]", "front_of_queue[mode=faithful]"]
    assert "+0.1 " in table[0] + " " and "-0.7*" in table[1] and "+13.3*" in table[2]
    for h in ("100ms", "1s", "10s", "60s", "5min"):
        assert h in next(ln for ln in lines if ln.startswith("model"))
    for e in doc["leaderboard"]:
        assert f"  {bias_sentence(e, 60 * 10**9)}" in lines
    assert (
        "At 60 s, front_of_queue[mode=faithful] fills 13.3 pp more often than the exact replay (95% CI +10.1 to " in out
    )
    assert "At 60 s, hbt_power3[n=2] fills 0.7 pp less often than the exact replay" in out
    assert "At 60 s, hbt_log fills 0.1 pp more often than the exact replay, within noise (95% CI -0.2" in out
    # The answer first: the closest and the furthest model at the sort horizon.
    assert lines[1] == (
        "reality gap at 60 s: from +0.1 pp (hbt_log, the closest) to +13.3 pp (front_of_queue[mode=faithful], "
        "the furthest), model minus exact fill rate"
    )
    # Another sort horizon on request (`sort_horizon`: the horizon itself when evaluated, else the nearest below).
    out10 = cli.render(["zoo"], doc, extra={"sort_horizon_ns": 10_000_000_000})
    assert "sorted by |fill-rate bias| at 10s" in out10
    out30 = cli.render(["zoo"], doc, extra={"sort_horizon_ns": 30_000_000_000})
    assert "sorted by |fill-rate bias| at 10s" in out30


def test_zoo_view_reports_the_never_cancel_population(tmp_path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    pq.write_table(pa.table({"never_cancel": [True, True, False]}), tmp_path / "zoo_shadows.parquet")
    pq.write_table(
        pa.table(
            {
                "model_id": ["hbt_log", "hbt_log"],
                "metric": ["fill_rate_bias", "discordance_p_primary"],
                "horizon_ns": [60_000_000_000, 300_000_000_000],
                "stratum_kind": ["all", "all"],
                "stratum": ["all", "all"],
                "value": [0.001, 0.42],
                "ci_lo": [None, None],
                "ci_hi": [None, None],
                "n": [2, 2],
            }
        ),
        tmp_path / "zoo_errors.parquet",
    )
    out = cli.render(["zoo"], _zoo_doc(), args={"out": str(tmp_path)})
    assert "leaderboard population: 2 never-cancel shadows (planned life >= h_max 5min) of 3" in out
    assert "n = 2" in out and "at 5min cluster-robust discordance p = 0.42" in out


def test_empty_lists_read_none() -> None:
    out = cli.render_human(["doctor"], {"schema": "qt.doctor", "schemas_ok": True, "problems": []})
    assert "problems: none" in out.splitlines()


def test_views_fall_back_to_the_plain_dump() -> None:
    doc = {**_zoo_doc(), "leaderboard": []}
    assert cli.render(["zoo"], doc) == cli.render_human(["zoo"], doc)
    broken = {"schema": "qt.scorecard_summary", "bias": "nonsense"}
    assert cli.render(["score"], broken) == cli.render_human(["score"], broken)
    verbose = cli.render(["zoo"], _zoo_doc(), verbose=True)
    assert "\n\nall fields:\nzoo (qt.zoo_summary)" in verbose


def _score_doc(**kw: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema": "qt.scorecard_summary",
        "schema_version": 1,
        "run_id": "0123456789abcdef",
        "spec": {"gates": {"max_abs_fill_rate_bias": None, "require_equivalence": False}},
        "live_source": "live",
        "sim_source": {"kind": "zoo", "model_id": "hbt_power3[n=2]", "path": None},
        "n_orders": 200,
        "n_matched": 200,
        "n_unmatched_live": 0,
        "n_unmatched_sim": 0,
        "two_by_two": {"both": 85, "neither": 90, "live_only": 24, "sim_only": 1},
        "bias": {
            "fill_rate_bias": -0.115,
            "ci_lo": -0.152,
            "ci_hi": -0.079,
            "direction": "sim_underfills",
        },
        "mcnemar": {"p_exact": 1e-6},
        "equivalence": {"fill_rate_margin": 0.02, "tost_p": 0.9, "fill_rate_equivalent": False},
        "verdict": {"headline": "x", "signed_bias": -0.115, "gates_passed": True, "failed_gates": []},
        "discordance_clustered": {"p_value": 6.6e-6, "n_clusters": 26, "method": "obuchowski"},
        "files": [],
        "warnings": [],
    }
    doc.update(kw)
    return doc


def test_score_view_leads_with_the_headline_in_plain_words() -> None:
    out = cli.render(["score"], _score_doc(), extra={"window_ns": 30_000_000_000})
    lines = out.splitlines()
    assert lines[0] == "score: zoo model hbt_power3[n=2] vs the live log live (run 0123456789abcdef)"
    assert lines[1] == (
        "RESULT: the simulator fills 11.5 pp less often than live (95% CI -15.2 to -7.9 pp) within 30s of entry; "
        "significant (cluster-robust p = 6.6e-06, 26 clusters); plain McNemar p = 1e-06 assumes independent orders"
    )
    assert "-11.5" not in lines[1]  # no double negative
    assert "equivalence: NOT shown within +/-2.0 pp" in out
    assert "orders: 200 of 200 live orders scored" in out
    # No gates requested: never "passed".
    assert "gates: none set" in out and "passed" not in out
    assert "live filled" in out and "live not filled" in out


def test_score_view_gate_status_only_with_gates() -> None:
    gated = {"gates": {"max_abs_fill_rate_bias": 0.05, "require_equivalence": False}}
    ok = cli.render(["score"], _score_doc(spec=gated))
    assert "gates: passed" in ok
    failed = _score_doc(
        spec=gated,
        verdict={"headline": "x", "signed_bias": -0.115, "gates_passed": False, "failed_gates": ["max_abs"]},
    )
    assert "gates: FAILED (max_abs)" in cli.render(["score"], failed)


def test_score_view_without_cluster_test_reads_score_tests(tmp_path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    pq.write_table(
        pa.table(
            {
                "test_id": ["discordance_clustered"],
                "stratum_kind": ["all"],
                "p_value": [0.571],
                "note": ["obuchowski; clusters=26; discordant clusters=3"],
            }
        ),
        tmp_path / "score_tests.parquet",
    )
    doc = _score_doc(
        discordance_clustered=None,
        bias={"fill_rate_bias": 0.005, "ci_lo": -0.005, "ci_hi": 0.018, "direction": "no_significant_bias"},
    )
    out = cli.render(["score"], doc, args={"out": str(tmp_path)})
    assert "fills 0.5 pp more often than live" in out
    assert "not significant (cluster-robust p = 0.571, 26 clusters)" in out
    assert "within 60s of entry" in out


def _cal_doc(**kw: Any) -> dict[str, Any]:
    fitted = "hbt_power3[n=0.79]"
    default = "hbt_power3[n=2]"
    doc: dict[str, Any] = {
        "schema": "qt.calibration_result",
        "schema_version": 1,
        "run_id": "0123456789abcdef",
        "spec": {"kind": "alloc", "family": "power3", "grid": {"alloc": {"lo": -3.0, "hi": 4.0, "n": 141}}},
        "kind": "alloc",
        "family": "power3",
        "mode": "faithful",
        "param_name": "n",
        "estimate": 0.79,
        "ci_profile": [0.71, 0.88],
        "ci_bootstrap": [0.80, 0.96],
        "n_events": 241,
        "n_shadows": 64,
        "train": {"dates": ["2026-06-15"], "blocks": None},
        "test": {"dates": ["2026-06-16"], "blocks": None},
        "held_out": {fitted: {"brier": {}, "logloss": {"10000000000": 0.0277, "300000000000": 0.1022},
                              "fill_rate_bias": {"10000000000": 0.01, "300000000000": 0.02}}},
        "baseline_held_out": {default: {"brier": {}, "logloss": {"10000000000": 0.0139, "300000000000": 0.0876},
                                        "fill_rate_bias": {"10000000000": 0.0, "300000000000": 0.0}}},
        "stratified": [],
        "exports": {"hftbacktest": {"python": "x", "notes": []}, "nautilus": None, "latency_npz": None},
        "data_sources": [],
        "warnings": [],
    }  # fmt: skip
    doc.update(kw)
    return doc


def test_calibration_view_says_when_the_fit_loses_to_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    out = cli.render(["calibrate", "alloc"], _cal_doc())
    lines = out.splitlines()
    assert lines[1] == (
        "fitted: n = 0.79 (profile 95% CI 0.71 to 0.88; bootstrap 0.8 to 0.96) on 2026-06-15: 241 events, 64 shadows"
    )
    rows = {ln.split()[0]: ln.split()[-1] for ln in lines if ln.startswith(("10s", "5min"))}
    assert rows == {"10s": "NO", "5min": "NO"}
    assert "flags: none" in out
    # The export's own assessment (queuetruth.export.trust) gives the verdict when it is available ...
    assert "verdict: DO NOT USE (export refuses it): " in out and "WORSE at 10s" in out
    # ... else the view's own comparison of the held-out log loss.
    monkeypatch.setattr(cli, "_trust", lambda doc: None)
    out = cli.render(["calibrate", "alloc"], _cal_doc())
    assert "verdict: DO NOT USE: the fitted value is worse than the default at 10s, 5min" in out


def test_calibration_view_beats_ties_and_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_trust", lambda doc: None)
    better = _cal_doc()
    better["held_out"] = {"f": {"logloss": {"10000000000": 0.01, "300000000000": 0.0876}, "fill_rate_bias": {}}}
    out = cli.render(["calibrate", "alloc"], better)
    assert "verdict: beats the default at 10s and never loses" in out
    tie = _cal_doc()
    tie["held_out"] = {"f": tie["baseline_held_out"]["hbt_power3[n=2]"]}
    assert "verdict: no gain" in cli.render(["calibrate", "alloc"], tie)
    # D1/E1: the grid edge, a zero-width bootstrap CI and a one-sided profile CI are flagged; the verdict refuses.
    edge = _cal_doc(estimate=0.0497871, ci_bootstrap=[0.0497871, 0.0497871], ci_profile=[None, 1.0])
    out = cli.render(["calibrate", "alloc"], edge)
    assert "at the lower grid edge (n = 0.04979)" in out
    assert "zero-width bootstrap CI" in out and "profile CI open on one side" in out
    assert "verdict: DO NOT USE: at the lower grid edge" in out
    # Fields a calibration may report itself are honoured.
    flagged = _cal_doc(identified=False, plateau=[0.22, 0.47])
    out = cli.render(["calibrate", "alloc"], {**flagged})
    assert "not identified" in out and "plateau over n in [0.22, 0.47]" in out
    live = _cal_doc(
        kind="live",
        spec={"window_ns": 60_000_000_000, "family": "power3", "live": {"path": "live"}},
        held_out_discordance={"fill_rate_bias": -0.07, "ci_lo": -0.112, "ci_hi": -0.023, "p_clustered": 0.014,
                              "n": 80, "n_clusters": 6},
    )  # fmt: skip
    out = cli.render(["calibrate-live"], live)
    assert "241 events, 64 live orders" in out
    assert "held-out live orders: fitted model fill-rate bias -7.0 pp (95% CI -11.2 to -2.3)" in out
    # With the export's assessment: the grid-edge flag and the refusal come from it.
    monkeypatch.undo()
    out = cli.render(["calibrate", "alloc"], edge)
    assert "grid edge" in out and "verdict: DO NOT USE (export refuses it): " in out
    # The calibration's own `trust:` lines (recorded by queuetruth.export.trust.annotate) drive the table and verdict.
    rec = _cal_doc(
        warnings=[
            "trust: at_grid_edge: the maximum is at the lower end of the grid",
            "trust: beats_default: 10000000000=better,300000000000=mixed (x)",
            "trust: verdict: refused (at_grid_edge)",
        ]
    )
    out = cli.render(["calibrate", "alloc"], rec)
    rows = {ln.split()[0]: ln.split()[-1] for ln in out.splitlines() if ln.startswith(("10s", "5min"))}
    assert rows == {"10s": "yes", "5min": "mixed"}
    assert "flags: at grid edge: the maximum is at the lower end of the grid" in out
    assert "verdict: DO NOT USE (export refuses it: at_grid_edge): " in out
    # The structured record (schema 1.1) wins over the lines.
    st = _cal_doc(
        warnings=["trust: verdict: exportable"],
        trust={"verdict": "refused", "reasons": ["few_events"], "flags": ["plateau"],
               "beats_default": {"10000000000": "worse", "300000000000": "tie"}},
    )  # fmt: skip
    out = cli.render(["calibrate", "alloc"], st)
    rows = {ln.split()[0]: ln.split()[-1] for ln in out.splitlines() if ln.startswith(("10s", "5min"))}
    assert rows == {"10s": "NO", "5min": "tie"} and "plateau" in out
    assert "verdict: DO NOT USE (export refuses it: few_events): " in out
    ok = _cal_doc(warnings=["trust: beats_default: 10000000000=better (x)", "trust: verdict: exportable"])
    ok["held_out"] = better["held_out"]
    assert "verdict: exportable: beats the default at 10s" in cli.render(["calibrate", "alloc"], ok)


def test_calibration_view_latency() -> None:
    q = {"p10": 1e5, "p50": 2e5, "p90": 1e6, "p99": 5e6, "n": 10}
    doc = _cal_doc(kind="latency", family="latency", latency={k: q for k in ("entry_ns", "response_ns", "cancel_ns",
                                                                             "amend_ns")})  # fmt: skip
    out = cli.render(["calibrate", "latency"], doc)
    assert "entry" in out and "200 us" in out and "synchronized clocks" in out


# ---- exit codes: never success for nothing ------------------------------------------------------------------------


def _cmd(path: list[str], args: dict[str, Any], imp: str = "python") -> dict[str, Any]:
    return {
        "path": path,
        "args": args,
        "implementation": imp,
        "global": {"format": "json", "log_level": "warn", "threads": 1, "seed": 0, "quiet": True},
    }


def test_zero_matched_scorecard_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import BaseModel, ConfigDict

    from queuetruth import api

    class Fake(BaseModel):
        model_config = ConfigDict(extra="allow")

    doc = _score_doc(n_matched=0, n_unmatched_live=5, warnings=["schedule: 4 orders without instrument_id"])
    summary = Fake(**doc)
    monkeypatch.setattr(api, "score", lambda spec, **kw: summary)
    with pytest.raises(QtError) as e:
        cli._dispatch(_cmd(["score"], {"live": {"path": "l"}, "sim": {"kind": "exact"}}))
    assert e.value.code == "QT-INPUT-002" and e.value.exit_code == 2
    assert e.value.message.startswith("NOT SCORED: 0 of 200 live orders could be scored")
    assert "without instrument_id" in e.value.message and "instrument-map" in (e.value.hint or "")
    assert e.value.details["summary"]["n_matched"] == 0


def test_empty_calibration_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from queuetruth import api

    monkeypatch.setattr(api, "calibrate", lambda spec: _cal_doc(n_events=0, n_shadows=0))
    with pytest.raises(QtError) as e:
        cli._dispatch(_cmd(["calibrate", "alloc"], {}))
    assert e.value.code == "QT-INPUT-002" and "fitted on 0 events (train window 2026-06-15)" in e.value.message
    monkeypatch.setattr(api, "calibrate_live", lambda spec: _cal_doc(n_events=0, estimate=None))
    with pytest.raises(QtError, match="fitted on 0 events"):
        cli._dispatch(_cmd(["calibrate-live"], {}))
    # open conflict 12: no `refused` / `usable` keys exist on qt.calibration_result; a fitted calibration whose trust
    # verdict is refused is returned (export refuses it with QT-CALIB-001), never an error at calibrate time
    trust = {"verdict": "refused", "reasons": ["few_events"], "flags": [], "beats_default": {}}
    monkeypatch.setattr(api, "calibrate_live", lambda spec: _cal_doc(trust=trust))
    doc, gate = cli._dispatch(_cmd(["calibrate-live"], {}))
    assert doc is not None and doc["trust"]["verdict"] == "refused" and gate is False
    monkeypatch.setattr(api, "calibrate", lambda spec: _cal_doc())
    doc, gate = cli._dispatch(_cmd(["calibrate", "alloc"], {}))
    assert doc is not None and doc["estimate"] == 0.79 and gate is False


@pytest.mark.requires_core
def test_calibration_refusal_errors_propagate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refusal raised by the calibration or the export (e.g. a `QtError` subclass with its own exit code) reaches
    the exit code and the `qt.error` document unchanged."""
    from queuetruth import api

    class Refused(QtError):
        @property
        def exit_code(self) -> int:
            return 1

    def refuse(spec: Any, **kw: Any) -> Any:
        raise Refused("QT-CALIB-001", "export refused: worse than the default on held-out data", "--force")

    monkeypatch.setattr(api, "export", refuse)
    code = cli.main(["--format", "json", "export", "hftbacktest", "--calibration", "c.json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 1 and doc["code"] == "QT-CALIB-001" and doc["hint"] == "--force"


@pytest.mark.requires_core
def test_info_lines_are_not_warnings(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from queuetruth import api

    monkeypatch.setattr(api, "calibrate", lambda spec: _cal_doc(warnings=["info: comparison loglik", "held_out: x"]))
    argv = ["calibrate", "alloc", "--family", "power3", "--data", "d", "--shadows", "s", "--out", "o"]
    assert cli.main(argv) == 0
    err = capsys.readouterr().err
    assert "warning: held_out: x" in err and "info: comparison" not in err
    assert cli.main(["--log-level", "info", *argv]) == 0
    err = capsys.readouterr().err
    assert "\ninfo: comparison loglik\n" in "\n" + err and "warning: info:" not in err


def test_export_force_reaches_the_exporter(monkeypatch: pytest.MonkeyPatch) -> None:
    import queuetruth.export as qexp
    from queuetruth import api
    from queuetruth.schemas import models as m

    seen: dict[str, Any] = {}

    def run_export(spec: Any, *, force: bool = False) -> m.ExportSummary:
        seen["force"] = force or spec.force
        return m.ExportSummary(target="hftbacktest", files=[], text="x")

    monkeypatch.setattr(qexp, "run_export", run_export)
    api.export({"target": "hftbacktest", "calibration": {"path": "c"}}, force=True)
    assert seen == {"force": True}
    doc, _ = cli._dispatch(
        _cmd(["export", "hftbacktest"], {"target": "hftbacktest", "calibration": {"path": "c"}, "force": True})
    )
    assert doc is not None and doc["text"] == "x" and seen == {"force": True}


# ---- new commands ---------------------------------------------------------------------------------------------------


def test_live_import_sim_venues_route_to_import_sim(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from queuetruth import live as qlive

    seen: dict[str, Any] = {}

    def fake(kind: str, inputs: list[str], out: str, **kw: Any) -> Any:
        seen.update(kind=kind, inputs=inputs, out=out, **kw)
        raise QtError("QT-INPUT-001", "stop here")

    monkeypatch.setattr(qlive, "import_sim", fake)
    monkeypatch.setattr(qlive, "IMPORTERS", {k: v for k, v in qlive.IMPORTERS.items() if not k.startswith("sim_")})
    args = {
        "venue": "sim_nautilus",
        "inputs": [{"path": "orders.csv"}],
        "out": str(tmp_path / "simlog"),
        "account": "BT",
        "instrument_map": {"path": "map.csv"},
        "_cli": {"options": {"venue": "binance", "size_exp": -8}},
    }
    with pytest.raises(QtError, match="stop here"):
        cli._dispatch(_cmd(["live", "import"], args))
    assert seen == {
        "kind": "nautilus",
        "inputs": ["orders.csv"],
        "out": str(tmp_path / "simlog"),
        "account": "BT",
        "instrument_map": "map.csv",
        "append": False,
        "venue": "binance",
        "size_exp": -8,
    }
    from queuetruth import api

    with pytest.raises(QtError, match="apply to simulator imports"):
        api.import_live({"venue": "binance", "inputs": [{"path": "a"}], "out": "o"}, size_exp=-8)
    with pytest.raises(QtError, match="tz does not apply"):
        api.import_live({"venue": "sim_hftbacktest", "inputs": [{"path": "a"}], "out": "o", "tz": "UTC"})


def test_live_import_passes_spec_tz_to_the_importer(monkeypatch: pytest.MonkeyPatch) -> None:
    from queuetruth import api
    from queuetruth import live as qlive

    seen: dict[str, Any] = {}

    def fake(spec: Any, *, tz: str | None = None) -> Any:
        seen.update(tz=tz, fix=spec.fix.model_dump())
        raise QtError("QT-INPUT-001", "stop")

    monkeypatch.setattr(qlive, "import_venue", fake)
    spec = {
        "venue": "fix44",
        "inputs": [{"path": "a"}],
        "out": "o",
        "tz": "America/Chicago",
        "fix": {"delimiter": "|", "local_ts_regex": "^(\\S+)", "venue_name": "cb"},
    }
    with pytest.raises(QtError):
        api.import_live(spec)
    want = {"delimiter": "|", "local_ts_regex": "^(\\S+)", "venue_name": "cb"}
    assert seen["tz"] == "America/Chicago" and {k: seen["fix"][k] for k in want} == want


def test_export_replay_schedule_is_stub_tolerant(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import queuetruth.export as qexp
    from queuetruth import api

    for name in api.REPLAY_SCHEDULE_FUNCTIONS:
        monkeypatch.delattr(qexp, name, raising=False)
    with pytest.raises(QtError) as e:
        api.export_replay_schedule("nautilus", out=str(tmp_path / "s.py"), live="live")
    assert e.value.code == "QT-USAGE-001" and "schedule-replay" in e.value.message
    seen: dict[str, Any] = {}

    from dataclasses import dataclass, field

    @dataclass
    class Result:
        target: str
        path: str
        notes: list[str] = field(default_factory=list)

    def replay_schedule(target: str, out: str, *, schedule: str | None = None, live: str | None = None) -> Result:
        seen.update(target=target, out=out, schedule=schedule, live=live)
        Path(out).write_text("# replay\n")
        return Result(target, out, ["run it in your simulator"])

    monkeypatch.setattr(qexp, "replay_schedule", replay_schedule, raising=False)
    out = tmp_path / "strategy.py"
    with pytest.warns(UserWarning, match="run it in your simulator"):
        doc, gate = cli._dispatch(
            _cmd(["export", "replay-schedule"], {"target": "nautilus", "live": {"path": "live"}, "out": str(out)})
        )
    assert seen == {"target": "nautilus", "out": str(out), "schedule": None, "live": "live"}
    assert doc is not None and gate is False
    assert doc["schema"] == "qt.export_summary" and doc["target"] == "nautilus"
    assert [f["name"] for f in doc["files"]] == ["strategy.py"] and doc["text"] is None
    with pytest.raises(QtError, match="does not take amends"):
        api.export_replay_schedule("nautilus", out=str(out), schedule="s", amends="a")


def test_mcp_serve_without_the_extra(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setitem(sys.modules, "anyio", None)
    monkeypatch.delitem(sys.modules, "queuetruth.mcp.server", raising=False)
    with pytest.raises(QtError) as e:
        cli._dispatch(_cmd(["mcp", "serve"], {"local": True}))
    assert e.value.code == "QT-USAGE-001"
    assert e.value.message == 'mcp serve needs the MCP extra: pip install "queuetruth[mcp]"'


def test_jobs_without_the_client_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "httpx", None)
    for k in [k for k in sys.modules if k.startswith("queuetruth.client")]:
        monkeypatch.delitem(sys.modules, k)
    with pytest.raises(QtError) as e:
        cli._dispatch(_cmd(["jobs", "status"], {"id": "job_1"}))
    assert e.value.code == "QT-USAGE-001" and 'pip install "queuetruth[client]"' in e.value.message


def test_hazard_model_is_imported_lazily() -> None:
    import subprocess

    code = (
        "import sys, queuetruth as qt; assert 'sklearn' not in sys.modules, 'eager'; "
        "assert 'HazardModel' in dir(qt); m = qt.HazardModel; assert 'sklearn' in sys.modules; print(m.__name__)"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "HazardModel"


# ---- end to end on synth_small (gate tests) -------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_core
def test_human_output_end_to_end(repo_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import pyarrow.parquet as pq

    ds = str(repo_root / "tests" / "fixtures" / "synth_small")
    live = str(repo_root / "tests" / "fixtures" / "synth_small" / "own_orders" / "live")
    sh = str(tmp_path / "sh.parquet")

    def run(*argv: str) -> tuple[int, str, str]:
        code = cli.main(list(argv))
        o = capsys.readouterr()
        return code, o.out, o.err

    assert run("shadows", "sample", "--data", ds, "--n", "120", "--out", sh, "--seed", "3")[0] == 0
    zoo = str(tmp_path / "zoo")
    code, out, err = run(
        "zoo", "--data", ds, "--shadows", sh, "--out", zoo, "--models", "front_of_queue,hbt_power3[n=2]",
        "--bootstrap-b", "20", "--write-trajectory",
    )  # fmt: skip
    assert code == 0, err
    assert (
        "leaderboard population: " in out
        and "never-cancel shadows" in out
        and "sorted by |fill-rate bias| at 60s" in out
    )
    assert "in plain words:" in out and "At 60 s, front_of_queue[mode=faithful] " in out
    code, out10, _ = run("zoo", "--data", ds, "--shadows", sh, "--out", str(tmp_path / "zoo10"), "--models",
                         "front_of_queue", "--bootstrap-b", "5", "--sort-horizon", "10s", "--verbose")  # fmt: skip
    assert code == 0 and "sorted by |fill-rate bias| at 10s" in out10 and "\nall fields:\nzoo (qt.zoo_summary)" in out10

    sid = pq.read_table(sh).column("shadow_id")[0].as_py()
    code, out, err = run("why", "--data", ds, "--shadows", sh, "--shadow-id", sid, "--zoo", zoo)
    assert code == 0, err
    assert out.startswith(f"why: shadow {sid} | QT") and "time (UTC)" in out and "conclusion: " in out
    assert "model: " in out and " model" in out.splitlines()[4]
    assert "000000000" not in out  # decimal prices, not fixed-point integers
    code, out, err = run("--format", "json", "why", "--data", ds, "--shadows", sh, "--shadow-id", sid, "--model",
                         "front_of_queue")  # fmt: skip
    doc = json.loads(out)
    assert code == 0 and doc["schema"] == "qt.why_trace"
    assert any("model_ahead=" in (e["note"] or "") for e in doc["events"]) or not doc["events"]

    key = pq.read_table(repo_root / "tests" / "fixtures" / "synth_small" / "own_orders" / "schedule.parquet")
    order_key = key.column("order_key")[0].as_py()
    code, out, err = run("why", "--data", ds, "--live", live, "--order-key", order_key, "--model", "hbt_power3[n=2]")
    assert code == 0, err
    assert f"why: live order {order_key}" in out and "own order removed from the tape" in out

    code, out, err = run("score", "--live", live, "--sim-model", "hbt_power3[n=2]", "--data", ds, "--out",
                         str(tmp_path / "sc"), "--bootstrap-b", "30", "--perm-b", "30")  # fmt: skip
    assert code == 0, err
    lines = out.splitlines()
    assert lines[0].startswith("score: zoo model hbt_power3") and lines[1].startswith("RESULT: the simulator fills ")
    assert "within 60s of entry" in lines[1] and "cluster-robust p = " in lines[1]
    assert "gates: none set" in out and "Gates passed" not in out and "gates: passed" not in out
    sc2 = str(tmp_path / "sc2")
    code, out, _ = run("score", "--live", live, "--sim-model", "front_of_queue", "--data", ds, "--out", sc2,
                       "--bootstrap-b", "30", "--perm-b", "30", "--fail-abs-bias", "0")  # fmt: skip
    assert code == 1 and "gates: FAILED (max_abs_fill_rate_bias)" in out

    cal = ["calibrate", "alloc", "--family", "power3", "--data", ds, "--shadows", sh, "--bootstrap-b", "10"]
    code, out, err = run(*cal, "--out", str(tmp_path / "cal"), "--train", "2026-06-15", "--test", "2026-06-16")
    if code == 0:
        assert "\nfitted: n = " in out and "beats default" in out and "\nflags: " in out and "\nverdict: " in out
    else:  # a calibration may refuse itself (fail-safe): an error with its reason, never a silent success
        assert code in (1, 2) and err.startswith("QT-")
    # D1: a train window outside the data fits nothing: never exit 0.
    code, out, err = run(*cal, "--out", str(tmp_path / "cal0"), "--train", "2026-06-17", "--test", "2026-06-16")
    assert code != 0 and err.startswith("QT-"), (out, err)


@pytest.mark.integration
@pytest.mark.requires_core
def test_export_replay_schedule_end_to_end(repo_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """With the schedule-replay adapters (`queuetruth.export.replay_schedule`): a NautilusTrader strategy and an
    hftbacktest script for the synthetic live log."""
    synth = repo_root / "tests" / "fixtures" / "synth_small"
    live = str(synth / "own_orders" / "live")
    for target, name in (("nautilus", "strategy.py"), ("hftbacktest", "loop.py")):
        out = tmp_path / name
        argv = ["--format", "json", "export", "replay-schedule", "--target", target, "--live", live]
        code = cli.main([*argv, "--data", str(synth), "--out", str(out)])
        doc = json.loads(capsys.readouterr().out)
        assert code == 0, doc
        assert doc["schema"] == "qt.export_summary" and doc["target"] == target
        assert [f["name"] for f in doc["files"]] == [name] and out.stat().st_size > 1000
        compile(out.read_text(), str(out), "exec")  # valid Python


@pytest.mark.requires_core
def test_resolve_order_ids_formats(repo_root: Path, tmp_path: Path) -> None:
    from queuetruth import api

    ds = repo_root / "tests" / "fixtures" / "synth_small"
    both = [{"instrument_id": 1, "order_id": 5}, {"instrument_id": 2, "order_id": 5}]
    (tmp_path / "ids.txt").write_text("# own orders\n5\n")
    (tmp_path / "ids.csv").write_text("instrument_id,order_id\n2,7\n1,7\n")
    (tmp_path / "ids.json").write_text(json.dumps([5, {"instrument_id": 2, "order_id": 7}]))
    assert api.resolve_order_ids(tmp_path / "ids.txt", ds) == both
    assert api.resolve_order_ids(str(tmp_path / "ids.csv"), ds) == [
        {"instrument_id": 1, "order_id": 7},
        {"instrument_id": 2, "order_id": 7},
    ]
    assert api.resolve_order_ids(tmp_path / "ids.json", ds) == [*both, {"instrument_id": 2, "order_id": 7}]
    assert api.resolve_order_ids([5], ds) == both
    dated = api.resolve_order_ids([{"order_id": 9, "session_date": "2026-06-16"}], ds)
    assert dated == [{"instrument_id": i, "order_id": 9, "session_date": "2026-06-16"} for i in (1, 2)]
    with pytest.raises(QtError, match="expected an order id"):
        api.resolve_order_ids([{"order_id": 9, "venue": "x"}], ds)
    with pytest.raises(QtError) as e:
        api.resolve_order_ids(tmp_path / "missing.txt", ds)
    assert e.value.code == "QT-INPUT-001"


@pytest.mark.integration
@pytest.mark.requires_core
def test_zoo_amends_and_exclude_order_ids(repo_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`zoo --amends FILE` -> `ZooSpec.amends`; `zoo --exclude-order-ids FILE` -> `ZooSpec.exclude_order_ids` (resolved
    from the file, so the ids enter the spec echo and `run_id`)."""
    import pyarrow.parquet as pq

    synth = repo_root / "tests" / "fixtures" / "synth_small"
    truth = synth / "truth"
    keys = pq.read_table(synth / "own_orders" / "schedule.parquet").column("order_key").to_pylist()
    ids = sorted({int(str(k).rsplit(":", 1)[-1]) for k in keys if k})
    (tmp_path / "own.txt").write_text("\n".join(map(str, ids)) + "\n")
    base = ["--format", "json", "zoo", "--data", str(synth), "--shadows", str(truth / "shadows.parquet")]
    base += ["--models", "front_of_queue", "--no-fills", "--bootstrap-b", "5"]
    code = cli.main([*base, "--out", str(tmp_path / "z0")])
    plain = json.loads(capsys.readouterr().out)
    assert code == 0 and plain["spec"].get("exclude_order_ids") in (None, [])
    argv = [*base, "--out", str(tmp_path / "z1"), "--amends", str(truth / "amends.parquet")]
    code = cli.main([*argv, "--exclude-order-ids", str(tmp_path / "own.txt")])
    doc = json.loads(capsys.readouterr().out)
    assert code == 0 and doc["schema"] == "qt.zoo_summary"
    assert doc["spec"]["amends"] == {"path": str(truth / "amends.parquet")}
    echo = [{k: v for k, v in it.items() if v is not None} for it in doc["spec"]["exclude_order_ids"]]
    assert echo == api_ids(synth, ids) and len(echo) == 2 * len(ids)
    assert doc["run_id"] != plain["run_id"]


def api_ids(synth: Path, ids: list[int]) -> list[dict[str, int]]:
    from queuetruth import api

    return api.resolve_order_ids(ids, synth)
