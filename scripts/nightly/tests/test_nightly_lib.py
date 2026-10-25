"""Unit tests of the nightly runner's library: versions and wheels, environments, history and diffs, exit codes,
gates and the page. Standard library + pytest only (run: pytest scripts/nightly/tests)."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qtnightly import EXIT_ADVISORY, EXIT_CHANGED, EXIT_ERROR, EXIT_FAIL, EXIT_OK
from qtnightly import envs as E
from qtnightly import evaluate as V
from qtnightly import history as H
from qtnightly import page as P

# ---------------------------------------------------------------------------------------------------------------
# versions and wheels
# ---------------------------------------------------------------------------------------------------------------


def test_version_key_orders_prereleases_before_the_final_release():
    vs = ["2.0.0rc7", "2.0.0", "2.0.0rc10", "2.0.0b1", "2.0.0a3", "2.0.1rc1", "1.231.0", "2.0.0.post1"]
    ordered = sorted(vs, key=E.version_key)
    assert ordered == ["1.231.0", "2.0.0a3", "2.0.0b1", "2.0.0rc7", "2.0.0rc10", "2.0.0", "2.0.0.post1", "2.0.1rc1"]


@pytest.mark.parametrize("v", ["2.0.0.dev1", "2.0", "latest", "2.0.0rc", "v2.0.0", ""])
def test_version_key_rejects_dev_and_malformed(v):
    assert E.version_key(v) is None


def _wheel(fn, sha="ab" * 32):
    return {"filename": fn, "url": f"https://files.example/{fn}", "digests": {"sha256": sha}, "size": 10}


def test_newest_installable_skips_yanked_empty_other_majors_and_releases_without_a_host_wheel():
    mac = "-cp313-cp313-macosx_14_0_arm64.whl"
    rel = {
        "1.231.0": [_wheel("n-1.231.0" + mac)],
        "2.0.0rc6": [_wheel("n-2.0.0rc6" + mac)],
        "2.0.0rc7": [_wheel("n-2.0.0rc7" + mac)],
        "2.0.0rc8": [{**_wheel("n-2.0.0rc8" + mac), "yanked": True}],  # every file yanked
        "2.0.0rc9": [],  # no files
        "2.0.0rc10": [_wheel("n-2.0.0rc10-cp314-cp314-macosx_14_0_arm64.whl")],  # no cp313 wheel yet
        "3.0.0a1": [_wheel("n-3.0.0a1" + mac)],
        "2.0.0.dev5": [_wheel("n-2.0.0.dev5" + mac)],
    }

    def ok(files):
        return E.wheel_for_host(files, "cp313", "Darwin", "arm64", 15) is not None

    assert E.newest_installable(rel, 2, ok) == "2.0.0rc7"
    assert E.newest_installable(rel, 2, lambda files: True) == "2.0.0rc10"
    assert E.newest_installable(rel, 1, ok) == "1.231.0"
    assert E.newest_installable(rel, 4, ok) is None


def test_wheel_for_host_off_macos():
    files = [
        _wheel("n-2.0.0-cp313-cp313-macosx_14_0_arm64.whl"),
        _wheel("n-2.0.0-cp313-cp313-manylinux_2_35_x86_64.whl"),
        _wheel("n-2.0.0-cp312-cp312-manylinux_2_35_aarch64.whl"),
    ]
    assert E.wheel_for_host(files, "cp313", "Linux", "x86_64", None)["filename"].endswith("x86_64.whl")
    assert E.wheel_for_host(files, "cp313", "Linux", "aarch64", None) is None
    assert E.wheel_for_host(files, "cp312", "Linux", "aarch64", None)["needs_retag"] is False


def test_pick_wheel_flags_a_tag_newer_than_the_host():
    files = [
        _wheel("nautilus_trader-1.231.0.tar.gz"),
        _wheel("nautilus_trader-1.231.0-cp312-cp312-macosx_26_0_arm64.whl"),
        _wheel("nautilus_trader-1.231.0-cp313-cp313-manylinux_2_35_x86_64.whl"),
        _wheel("nautilus_trader-1.231.0-cp313-cp313-macosx_26_0_arm64.whl"),
    ]
    w = E.pick_wheel(files, "cp313", "arm64", 15)
    assert w["filename"].endswith("cp313-cp313-macosx_26_0_arm64.whl")
    assert w["needs_retag"] is True
    assert E.pick_wheel(files, "cp313", "arm64", 26)["needs_retag"] is False
    assert E.pick_wheel(files, "cp311", "arm64", 15) is None
    assert E.pick_wheel(files, "cp313", "x86_64", 15) is None


def test_pick_wheel_skips_yanked_and_accepts_universal2():
    files = [
        {**_wheel("x-2.0.0rc7-cp313-cp313-macosx_11_0_arm64.whl"), "yanked": True},
        _wheel("x-2.0.0rc7-cp313-cp313-macosx_10_15_universal2.whl"),
    ]
    w = E.pick_wheel(files, "cp313", "arm64", 15)
    assert w["filename"].endswith("universal2.whl") and w["needs_retag"] is False


def test_retagged_name():
    assert (
        E.retagged_name("nautilus_trader-1.231.0-cp313-cp313-macosx_26_0_arm64.whl")
        == "nautilus_trader-1.231.0-cp313-cp313-macosx_11_0_arm64.whl"
    )


def test_find_wheel_uses_a_local_copy_only_when_its_digest_matches(tmp_path):
    body = b"wheel bytes"
    sha = hashlib.sha256(body).hexdigest()
    entry = {**_wheel("n-1.0.0-cp313-cp313-macosx_26_0_arm64.whl", sha), "needs_retag": True}
    good, bad, dest = tmp_path / "good", tmp_path / "bad", tmp_path / "dest"
    good.mkdir()
    bad.mkdir()
    (bad / "n-1.0.0-cp313-cp313-macosx_11_0_arm64.whl").write_bytes(b"tampered")
    (good / "n-1.0.0-cp313-cp313-macosx_11_0_arm64.whl").write_bytes(body)
    logs: list[str] = []
    p = E.find_or_download_wheel(entry, dest, [bad, good], offline=True, log=logs.append)
    assert p == good / "n-1.0.0-cp313-cp313-macosx_11_0_arm64.whl"
    assert any("ignoring" in m for m in logs)
    with pytest.raises(E.EnvError, match="offline"):
        E.find_or_download_wheel(entry, dest, [bad], offline=True, log=logs.append)


def test_exclude_newer_is_fourteen_days_back_at_midnight_utc():
    assert E.exclude_newer(dt.date(2026, 10, 24)) == "2026-10-10T00:00:00Z"
    assert E.exclude_newer(dt.date(2026, 3, 1), days=1) == "2026-02-28T00:00:00Z"


# ---------------------------------------------------------------------------------------------------------------
# environments
# ---------------------------------------------------------------------------------------------------------------


def _fake_run(calls, fail_on=None):
    def run(argv):
        calls.append(argv)
        if fail_on and fail_on in " ".join(map(str, argv)):
            return 1
        if argv[1] == "venv":
            v = Path(argv[-1])
            (v / "bin").mkdir(parents=True)
            (v / "bin" / "python").write_text("")
        return 0

    return run


def test_digest_ignores_unhashed_arguments():
    a = E.EnvSpec("x", "py", [["pkg==1"]], {"k": 1}, ["--exclude-newer", "2026-10-10T00:00:00Z"])
    b = E.EnvSpec("x", "py", [["pkg==1"]], {"k": 1}, ["--exclude-newer", "2026-10-11T00:00:00Z"])
    c = E.EnvSpec("x", "py", [["pkg==2"]], {"k": 1})
    assert a.digest() == b.digest() != c.digest()


def test_ensure_env_builds_reuses_and_rebuilds(tmp_path):
    venv = tmp_path / "venvs" / "x"
    spec = E.EnvSpec("x", "python3", [["a==1"], ["--no-deps", "b==2"]], {"k": 1}, ["--exclude-newer", "T"])
    calls: list = []
    py, rebuilt = E.ensure_env(venv, spec, "uv", _fake_run(calls), refresh=False, log=lambda m: None)
    assert rebuilt and py == venv / "bin" / "python"
    assert [c[1] for c in calls] == ["venv", "pip", "pip"]
    assert calls[1][-3:] == ["a==1", "--exclude-newer", "T"]
    assert "--no-config" in calls[1] and "--no-config" in calls[0]
    assert E.env_is_current(venv, spec)
    calls.clear()
    _, rebuilt = E.ensure_env(venv, spec, "uv", _fake_run(calls), refresh=False, log=lambda m: None)
    assert not rebuilt and calls == []
    _, rebuilt = E.ensure_env(venv, spec, "uv", _fake_run(calls), refresh=True, log=lambda m: None)
    assert rebuilt
    spec2 = E.EnvSpec("x", "python3", [["a==2"]], {"k": 1})
    assert not E.env_is_current(venv, spec2)


def test_ensure_env_failure_leaves_no_marker(tmp_path):
    venv = tmp_path / "v"
    spec = E.EnvSpec("x", "python3", [["broken==1"]])
    with pytest.raises(E.EnvError, match="broken"):
        E.ensure_env(venv, spec, "uv", _fake_run([], fail_on="broken"), refresh=False, log=lambda m: None)
    assert not (venv / E.MARKER).exists()
    assert not E.env_is_current(venv, spec)


def test_env_with_a_corrupt_marker_is_stale(tmp_path):
    venv = tmp_path / "v"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("")
    (venv / E.MARKER).write_text("{not json")
    assert not E.env_is_current(venv, E.EnvSpec("x", "p", []))


def test_python_tag_validates_the_probe():
    assert E.python_tag("py", lambda argv: "cp313\n") == "cp313"
    with pytest.raises(E.EnvError):
        E.python_tag("py", lambda argv: "Python 3.13")


# ---------------------------------------------------------------------------------------------------------------
# history, diff, exit code
# ---------------------------------------------------------------------------------------------------------------


def _run(nid, mode="reduced", checks=None):
    return {"nightly_id": nid, "mode": mode, "checks": checks or {}}


def _check(status="pass", required=True, metrics=None, noisy=None):
    return {"status": status, "required": required, "metrics": metrics or {}, "noisy": noisy or []}


def test_history_roundtrip_and_schema_errors(tmp_path):
    p = tmp_path / "history.json"
    assert H.load_history(p) == H.empty_history()
    doc = H.append_run(H.empty_history(), _run("20261024T000000Z"), keep=5)
    H.save_history(p, doc)
    assert H.load_history(p)["runs"][0]["nightly_id"] == "20261024T000000Z"
    assert not (tmp_path / "history.json.tmp").exists()
    p.write_text(json.dumps({"schema": "other", "runs": []}))
    with pytest.raises(ValueError, match="not a"):
        H.load_history(p)
    p.write_text(json.dumps({"schema": "qt.nightly.history", "schema_version": 99, "runs": []}))
    with pytest.raises(ValueError, match="schema_version"):
        H.load_history(p)


def test_append_run_trims_and_replaces():
    doc = H.empty_history()
    for i in range(5):
        doc = H.append_run(doc, _run(f"2026102{i}T000000Z"), keep=3)
    assert [r["nightly_id"] for r in doc["runs"]] == ["20261022T000000Z", "20261023T000000Z", "20261024T000000Z"]
    doc = H.append_run(doc, {**_run("20261023T000000Z"), "x": 1}, keep=3)
    assert [r["nightly_id"] for r in doc["runs"]] == ["20261022T000000Z", "20261024T000000Z", "20261023T000000Z"]
    with pytest.raises(ValueError):
        H.append_run(doc, _run("x"), keep=0)


def test_previous_run_is_per_mode():
    doc = {
        "runs": [
            _run("20261020T000000Z", "full"),
            _run("20261021T000000Z", "reduced"),
            _run("20261022T000000Z", "full"),
        ]
    }
    assert H.previous_run(doc, "full")["nightly_id"] == "20261022T000000Z"
    assert H.previous_run(doc, "full", before_id="20261022T000000Z")["nightly_id"] == "20261020T000000Z"
    assert H.previous_run(doc, "reduced")["nightly_id"] == "20261021T000000Z"
    assert H.previous_run(doc, "other") is None


def test_diff_without_previous_run_is_empty():
    d = H.diff_runs(None, _run("b", checks={"x": _check()}))
    assert d["vs"] is None and d["status_changes"] == d["changed"] == d["noisy"] == d["context"] == []
    other_mode = _run("a", mode="full", checks={"x": _check("fail")})
    assert H.diff_runs([other_mode], _run("b", checks={"x": _check()}))["vs"] is None


def test_diff_classifies_status_metric_and_noisy_changes():
    prev = _run(
        "a",
        checks={
            "x": _check(
                metrics={"n": 3, "rate": 0.5, "same": 1.0, "gone": 1, "wall": 10.0, "s": "v1", "flag": True},
                noisy=["wall"],
            ),
            "y": _check(),
            "old": _check(),
        },
    )
    cur = _run(
        "b",
        checks={
            "x": _check(
                metrics={"n": 4, "rate": 0.5 + 1e-15, "same": 1.0, "new": 2, "wall": 12.0, "s": "v2", "flag": 1},
                noisy=["wall"],
            ),
            "y": _check("fail"),
            "new": _check(),
        },
    )
    d = H.diff_runs(prev, cur)
    assert d["vs"] == "a"
    # "old" was not run (not a change); "new" has no earlier run (listed, not counted)
    assert [(s["check"], s["before"], s["after"]) for s in d["status_changes"]] == [("y", "pass", "fail")]
    assert d["new_checks"] == ["new"]
    changed = {e["metric"]: e for e in d["changed"]}
    assert set(changed) == {"n", "gone", "new", "s", "flag"}  # 1e-15 relative is the same float; True is not 1
    assert changed["gone"]["kind"] == "removed" and changed["new"]["kind"] == "added"
    assert [e["metric"] for e in d["noisy"]] == ["wall"]
    assert d["noisy"][0]["rel_change"] == pytest.approx(0.2)


def test_diff_float_tolerance_is_relative_at_every_magnitude():
    def changed(a, b):
        ra = _run("a", checks={"x": _check(metrics={"v": a})})
        rb = _run("b", checks={"x": _check(metrics={"v": b})})
        return bool(H.diff_runs(ra, rb)["changed"])

    assert changed(1e-15, 2e-15)  # a 2x change of a tiny number is a change
    assert not changed(1e-15, 1e-15 * (1 + 1e-13))
    assert changed(0.0, 1e-300)
    assert not changed(3e9, 3e9 * (1 + 1e-13))
    assert changed(3, 3.0000001) and not changed(3, 3.0)


def test_diff_treats_nan_as_equal_to_nan():
    a = _run("a", checks={"x": _check(metrics={"v": math.nan})})
    b = _run("b", checks={"x": _check(metrics={"v": math.nan})})
    assert H.diff_runs(a, b)["changed"] == []


def test_diff_baseline_is_per_check_across_partial_and_error_runs():
    full1 = _run("1", checks={"hbt": _check(metrics={"m": 1}), "nt": _check(metrics={"m": 1})})
    only_hbt = _run("2", checks={"hbt": _check(metrics={"m": 1})})  # --only hbt
    nt_error = _run("3", checks={"hbt": _check(metrics={"m": 1}), "nt": _check("error")})
    full4 = _run("4", checks={"hbt": _check(metrics={"m": 1}), "nt": _check(metrics={"m": 2})})
    d = H.diff_runs([full1, only_hbt], _run("2b", checks={"hbt": _check(metrics={"m": 1})}))
    assert d["status_changes"] == [] and d["changed"] == []  # not running nt is not a change
    d = H.diff_runs([full1, only_hbt, nt_error], full4)
    assert d["vs"] == "3"
    assert d["baselines"]["nt"] == {"status": "3", "metrics": "1"}
    assert [(s["check"], s["before"], s["after"]) for s in d["status_changes"]] == [("nt", "error", "pass")]
    assert d["changed"] == [{"check": "nt", "metric": "m", "before": 1, "after": 2, "vs": "1"}]
    # a check that errors now is not diffed metric by metric (it has no metrics), only by status
    d = H.diff_runs([full1], nt_error)
    assert d["changed"] == [] and [s["after"] for s in d["status_changes"]] == ["error"]


def test_diff_lists_provenance_changes_as_context_only():
    a = _run("a", checks={"r": {**_check(metrics={"n": 1}), "provenance": {"build.source_tree_sha256": "x"}}})
    b = _run("b", checks={"r": {**_check(metrics={"n": 1}), "provenance": {"build.source_tree_sha256": "y"}}})
    d = H.diff_runs(a, b)
    assert d["changed"] == [] and d["context"] == [
        {"check": "r", "what": "build.source_tree_sha256", "before": "x", "after": "y"}
    ]
    assert H.exit_code(b["checks"], d) == EXIT_OK


@pytest.mark.parametrize(
    ("checks", "diff", "runner_error", "want"),
    [
        ({"a": _check(), "b": _check(required=False)}, {}, False, EXIT_OK),
        ({"a": _check()}, {"changed": [{"x": 1}]}, False, EXIT_CHANGED),
        ({"a": _check()}, {"status_changes": [{"x": 1}]}, False, EXIT_CHANGED),
        ({"a": _check(), "b": _check("fail", required=False)}, {"changed": [1]}, False, EXIT_ADVISORY),
        ({"a": _check(), "b": _check("error", required=False)}, {}, False, EXIT_ADVISORY),
        ({"a": _check("fail"), "b": _check("error", required=False)}, {}, False, EXIT_FAIL),
        ({"a": _check("fail"), "b": _check("error")}, {}, False, EXIT_ERROR),
        ({"a": _check()}, {}, True, EXIT_ERROR),
        ({}, {}, False, EXIT_OK),
    ],
)
def test_exit_code_precedence(checks, diff, runner_error, want):
    assert H.exit_code(checks, diff, runner_error) == want


def test_flags_and_verdict_keep_what_the_exit_code_hides():
    checks = {"a": _check(), "b": _check("fail", required=False)}
    diff = {"changed": [{"x": 1}, {"x": 2}]}
    assert H.run_flags(checks, diff) == ["advisory", "changed"]
    assert H.exit_code(checks, diff) == EXIT_ADVISORY
    assert H.verdict(EXIT_ADVISORY, diff).endswith("(also 2 tracked change(s) against earlier runs)")
    assert "also" not in H.verdict(EXIT_CHANGED, diff)
    assert H.run_flags({"a": _check()}, {}, runner_error=True) == ["error"]


def test_flatten():
    got = H.flatten("p", {"b": [1, {"c": None}], "a": {"x": 1.5, "y": "s", "z": object()}})
    assert got == {"p.a.x": 1.5, "p.a.y": "s", "p.b[0]": 1, "p.b[1].c": None}


# ---------------------------------------------------------------------------------------------------------------
# gates
# ---------------------------------------------------------------------------------------------------------------


def _hz(agree=1.0, **kw):
    return {
        h: {"nt_rate": 0.1, "zoo_rate": 0.1, "exact_rate": 0.05, "agree": agree, "nt1_zoo0": 0, "nt0_zoo1": 0, **kw}
        for h in V.HORIZONS
    }


def _nt_report(version="1.231.0", queue_agree=1.0, unexplained=0, replica_ok=True):
    rv = {n: {"against": n, "agree": 10 if replica_ok else 9, "total": 10} for n in ("nt_touch", "nt_queue")}
    dc = {"touch": {}, "queue": ({"unexplained": {"n": unexplained}} if unexplained else {"cap_on_update": {"n": 2}})}
    return {
        "nautilus_trader": version,
        "n_shadows": 60,
        "configs": {
            "touch_p1": {"zoo_model": "nt_prob_touch[mode=faithful;p=1]", "horizons": _hz()},
            "touch_p0.29": {"zoo_model": "nt_prob_touch[mode=faithful;p=0.29]", "horizons": _hz(agree=math.nan)},
            "queue_p1": {"zoo_model": "nt_queue_l2[mode=faithful]", "horizons": _hz(agree=queue_agree)},
        },
        "replica_validation": rv,
        "discrepancy_classes": dc,
    }


def test_evaluate_nt_pinned_passes_and_records_metrics():
    gates, metrics, noisy, prov = V.evaluate_nt(_nt_report(), "1.231.0")
    assert all(g["ok"] for g in gates), gates
    assert metrics["touch_p1.1s.agree"] == 1.0
    assert metrics["touch_p0.29.1s.agree"] is None  # NaN is not JSON
    assert metrics["discrepancy.queue.cap_on_update"] == 2
    assert noisy == [] and prov == {"nt.version": "1.231.0"} and "nt.version" not in metrics


def test_evaluate_nt_gates_queue_agreement_on_1x_only():
    g1, *_ = V.evaluate_nt(_nt_report(queue_agree=0.99), "1.231.0")
    assert [g["name"] for g in g1 if not g["ok"]] == ["queue_p1 agrees with nt_queue_l2[mode=faithful] on every order"]
    g2, m2, *_ = V.evaluate_nt(_nt_report("2.0.0rc7", queue_agree=0.99), "2.0.0rc7")
    assert all(g["ok"] for g in g2)
    assert m2["queue_p1.60s.agree"] == 0.99


@pytest.mark.parametrize(
    ("report", "version", "failing"),
    [
        (_nt_report(), "1.230.0", "nautilus_trader version"),
        (_nt_report(unexplained=3), "1.231.0", "no unexplained disagreement"),
        (_nt_report(replica_ok=False), "1.231.0", "replica nt_queue reproduces nt_queue"),
    ],
)
def test_evaluate_nt_failures(report, version, failing):
    gates = V.evaluate_nt(report, version).gates
    assert failing in [g["name"] for g in gates if not g["ok"]]


def test_evaluate_nt_missing_pieces_fail():
    r = _nt_report()
    del r["configs"]["touch_p1"]
    del r["replica_validation"]
    gates = V.evaluate_nt(r, "1.231.0").gates
    bad = [g["name"] for g in gates if not g["ok"]]
    assert "touch_p1 ran" in bad and "rule-set replicas validated" in bad
    r = _nt_report()
    r["configs"]["d10_touch_p1"] = {"zoo_model": "m", "horizons": {"1s": {"agree": 1.0}}}
    gates = V.evaluate_nt(r, "1.231.0").gates
    assert any(not g["ok"] and g["name"].startswith("d10_touch_p1") for g in gates)


def _hbt_model(compared=10, agree=10, mism=None, orders=11, excluded=1, codes=(14,), not_resting=0):
    return {
        "orders": orders,
        "compared": compared,
        "agree_first_fill_ts": agree,
        "not_resting_in_hbt": not_resting,
        "excluded_after_error": excluded,
        "hbt_errors": [{"ts": 5, "error_code": c} for c in codes],
        "mismatches": mism or [],
        "horizons": {h: {"agree": compared, "hbt_filled": 1, "zoo_filled": 1} for h in V.HORIZONS},
    }


def _failing(report):
    return [g["name"].split(": ", 1)[1] for g in V.evaluate_hbt(report).gates if not g["ok"]]


def test_evaluate_hbt():
    rep = {
        "engine": "hftbacktest 2.4.4",
        "n_orders": 11,
        "dropped": {"improving_offset_crosses": 1},
        "models": {"m": _hbt_model()},
    }
    ev = V.evaluate_hbt(rep)
    assert all(g["ok"] for g in ev.gates), ev.gates
    assert ev.metrics["m.hbt_errors"] == 1 and ev.metrics["hbt.dropped.improving_offset_crosses"] == 1
    assert ev.provenance == {"hbt.engine": "hftbacktest 2.4.4"}
    rep["models"]["m"] = _hbt_model(agree=9, mism=[{"order_id": 3}])
    gates = V.evaluate_hbt(rep).gates
    assert not gates[0]["ok"] and "order_id" in gates[0]["detail"]
    rep["models"]["m"] = _hbt_model(compared=0, agree=0)
    assert not V.evaluate_hbt(rep).gates[0]["ok"]
    assert not V.evaluate_hbt({"models": {}}).gates[0]["ok"]


@pytest.mark.parametrize(
    ("model", "failing"),
    [
        # hftbacktest errored early: 1 of 53 orders compared, 52 excluded; agreement alone would pass
        (_hbt_model(compared=1, agree=1, orders=53, excluded=52), "at least 90% of orders compared"),
        (_hbt_model(codes=(7,)), "at least 90% of orders compared"),  # an error that is not #312
        (_hbt_model(compared=9, agree=9, orders=11, excluded=1, not_resting=1), "every order rested"),
    ],
)
def test_evaluate_hbt_coverage_gates(model, failing):
    bad = _failing({"models": {"m": model}})
    assert any(b.startswith(failing) for b in bad), bad


def _repro_doc(status="pass", extra=None):
    steps = {
        "subset/12302019": {"status": "pass", "checks": [{"name": "a", "ok": True}], "numbers": {}, "wall_s_step": 1.0},
        "replay/12302019": {
            "status": status,
            "checks": [{"name": "stats.records", "ok": status == "pass"}],
            "numbers": {"stats": {"records": 6103262}, "hist": [1, 2], "records_per_s": 2e6, "wall_s": 1.5},
            "wall_s_step": 2.0,
            "free_gib_after": 20.0,
        },
    }
    return {
        "steps": steps,
        "build": {"queuetruth_version": "0.1.1", "source_tree_sha256": {"sha256": "ab"}},
        "build_changed_during_run": False,
        **(extra or {}),
    }


def test_evaluate_repro_pass_and_metrics():
    gates, metrics, noisy, prov = V.evaluate_repro(_repro_doc(), 0, ["subset/12302019", "replay/12302019"])
    assert all(g["ok"] for g in gates)
    assert metrics["replay/12302019.numbers.stats.records"] == 6103262
    assert metrics["replay/12302019.numbers.hist[1]"] == 2
    assert prov["build.source_tree_sha256"] == "ab" and not any(k.startswith("build.") for k in metrics)
    assert "replay/12302019.wall_s_step" in noisy and "replay/12302019.free_gib_after" in noisy
    assert "replay/12302019.numbers.records_per_s" in noisy and "replay/12302019.numbers.wall_s" in noisy
    assert "replay/12302019.numbers.stats.records" not in noisy


@pytest.mark.parametrize(
    ("key", "timing"),
    [
        ("headline/01302020.numbers.zoo_wall_s.faithful", True),
        ("calib.numbers.alloc/power3_wall_s", True),
        ("hazard.numbers.l2_only_wall_s", True),
        ("ingest/12302019.numbers.records_per_s", True),
        ("calib.numbers.alloc/power3.estimate", False),
        ("replay/12302019.numbers.stats.records", False),
        ("headline/01302020.numbers.n_models.faithful", False),
    ],
)
def test_is_timing_looks_at_every_segment(key, timing):
    assert V.is_timing(key) is timing


def _full_mode_doc(night: int, out: str, src_hash: str):
    """A results.json shaped like a full-mode run (subset, headline, calib, hazard steps) whose timings, paths and
    build identity differ from night to night while every result is identical."""
    t = 1.0 + night
    return {
        "steps": {
            "subset/01302020": {
                "status": "pass",
                "checks": [{"name": "framed", "ok": True}],
                "numbers": {
                    # streamed into the run's output on night one, kept in <state>/itch after that
                    "subset": f"{out if night == 1 else '/state'}/itch/01302020.subset.AAPL-MSFT-SPY-TSLA.itch50.gz",
                    "streamed_now": night == 1,
                    "source_url": "https://emi.nasdaq.com/ITCH/Nasdaq%20ITCH/01302020.NASDAQ_ITCH50.gz",
                    "subset_framed_sha256": "87" * 32,
                    "subset_messages": 9363498,
                    "stream_wall_s": 300.0 * t,
                },
                "wall_s_step": t,
                "free_gib_after": 20.0 - t,
            },
            "headline/01302020": {
                "status": "recorded",
                "checks": [{"name": "faithful: 16 models with rows", "ok": True}],
                "numbers": {
                    "artifact": f"{out}/headline/headline-2020-01-30.json",
                    "exact_fill_rate": 0.4123,
                    "n_models": {"faithful": 16, "corrected": 16},
                    "zoo_wall_s": {"faithful": 10.0 * t, "corrected": 11.0 * t},
                    "zoo_warnings": {"faithful": [], "corrected": []},
                    "wall_s": 30.0 * t,
                },
                "wall_s_step": 31.0 * t,
            },
            "calib": {
                "status": "recorded",
                "checks": [{"name": "alloc/power3: finite estimate", "ok": True}],
                "numbers": {
                    "alloc/power3": {"estimate": 2.5, "ci_bootstrap": [2.1, 2.9], "n_events": 1234},
                    "alloc/power3_wall_s": 40.0 * t,
                    "export_hftbacktest_files": [f"{out}/work/calib/alloc_power3/export_hftbacktest.py"],
                },
                "wall_s_step": 41.0 * t,
            },
            "hazard": {
                "status": "recorded",
                "checks": [{"name": "split: test = day two", "ok": True}],
                "numbers": {
                    "features": {"n_orders": 40000, "n_rows": 400000},
                    "features_wall_s": 50.0 * t,
                    "l2_only_scores_test": {"bss_vs_climatology": {"1000000000": {"value": 0.07}}},
                    "l2_only_wall_s": 60.0 * t,
                },
                "wall_s_step": 61.0 * t,
            },
        },
        "build": {"queuetruth_version": "0.1.1", "source_tree_sha256": {"sha256": src_hash}},
        "build_changed_during_run": False,
    }


def test_full_mode_shaped_nights_with_new_timings_paths_and_source_hash_exit_zero():
    runs = []
    for night, (nid, src) in enumerate([("20261024T023000Z", "aa"), ("20261025T023000Z", "bb")], start=1):
        out = f"/state/runs/{nid}/checks/repro/out"
        ev = V.evaluate_repro(_full_mode_doc(night, out, src), 0, [], {out: "<out>", "/state": "<state>"})
        assert all(g["ok"] for g in ev.gates)
        runs.append(_run(nid, "full", {"repro": {**_check(metrics=ev.metrics, noisy=ev.noisy), **ev._asdict()}}))
    d = H.diff_runs(runs[:1], runs[1])
    assert d["changed"] == [] and d["status_changes"] == []
    assert {e["metric"] for e in d["noisy"]} >= {
        "headline/01302020.numbers.zoo_wall_s.faithful",
        "calib.numbers.alloc/power3_wall_s",
        "hazard.numbers.l2_only_wall_s",
    }
    assert {e["what"] for e in d["context"]} == {
        "build.source_tree_sha256",
        "subset/01302020.numbers.subset",
        "subset/01302020.numbers.streamed_now",
    }  # the headline artifact path is under <out> both nights: equal once scrubbed
    assert runs[1]["checks"]["repro"]["metrics"]["calib.numbers.export_hftbacktest_files[0]"] == (
        "<out>/work/calib/alloc_power3/export_hftbacktest.py"
    )
    assert H.exit_code(runs[1]["checks"], d) == EXIT_OK
    # a real change in a result still counts
    doc = _full_mode_doc(3, "/o", "cc")
    doc["steps"]["calib"]["numbers"]["alloc/power3"]["n_events"] = 1235
    ev = V.evaluate_repro(doc, 0, [], {"/o": "<out>"})
    cur = _run("20261026T023000Z", "full", {"repro": {**_check(metrics=ev.metrics, noisy=ev.noisy), **ev._asdict()}})
    d = H.diff_runs(runs, cur)
    assert [e["metric"] for e in d["changed"]] == ["calib.numbers.alloc/power3.n_events"]


@pytest.mark.parametrize(
    ("doc", "rc", "expected", "failing"),
    [
        (_repro_doc("fail"), 1, ["replay/12302019"], "step replay/12302019"),
        (_repro_doc(), 0, ["headline/12302019"], "step headline/12302019 ran"),
        (_repro_doc(extra={"aborted": "disk"}), 1, [], "not aborted"),
        (_repro_doc(extra={"build_changed_during_run": True}), 0, [], "build unchanged during the run"),
    ],
)
def test_evaluate_repro_failures(doc, rc, expected, failing):
    gates = V.evaluate_repro(doc, rc, expected).gates
    assert failing in [g["name"] for g in gates if not g["ok"]]


def _bench(met=True, derived_met=True):
    return {
        "suite": "quick",
        "cases": [
            {
                "id": "c1",
                "status": "ok",
                "summary": {"median": 1.5e7},
                "target": {"op": ">=", "value": 8e6, "met": met},
            },
            {"id": "c2", "status": "ok", "summary": {"median": 2.0}, "target": None},
        ],
        "derived": [
            {
                "id": "bind.fixture.py_vs_cli",
                "kind": "cost_ratio",
                "summary": {"median": 1.02 if derived_met else 1.2},
                "target": {"op": "<=", "value": 1.05, "met": derived_met},
            },
            {"id": "core.synth2m.info", "kind": "throughput_loss", "summary": {"median": 0.1}, "target": None},
        ],
    }


def test_evaluate_bench():
    gates, metrics, noisy, prov = V.evaluate_bench(_bench(), 0, None, None)
    assert all(g["ok"] for g in gates) and metrics["bench.vs_previous"] == "no previous run"
    assert metrics["bench.case.c1.median"] == 1.5e7 and "bench.case." in noisy and prov["bench.baseline"] == "none"
    assert metrics["bench.derived.bind.fixture.py_vs_cli.median"] == 1.02 and "bench.derived." in noisy
    assert not all(g["ok"] for g in V.evaluate_bench(_bench(met=False), 0, None, None).gates)
    chk = {"rows": [{"id": "c1", "status": "regression"}], "env_reasons": []}
    gates, metrics, _, prov = V.evaluate_bench(_bench(), 0, chk, 1, "run 20261023T023000Z")
    assert not gates[-1]["ok"] and "c1" in gates[-1]["detail"] and metrics["bench.vs_previous.c1"] == "regression"
    assert "run 20261023T023000Z" in gates[-1]["detail"] and prov["bench.baseline"] == "run 20261023T023000Z"
    gates = V.evaluate_bench(_bench(), 0, {"rows": [], "env_reasons": ["power source differs"]}, 3).gates
    assert gates[-1]["ok"] and "power source" in gates[-1]["detail"]
    gates = V.evaluate_bench(None, 1, None, None).gates
    assert [g["ok"] for g in gates] == [False, False]


def test_evaluate_bench_gates_derived_ratio_targets():
    bad = [g["name"] for g in V.evaluate_bench(_bench(derived_met=False), 0, None, None).gates if not g["ok"]]
    assert bad == ["bind.fixture.py_vs_cli (cost_ratio) target <= 1.05"]


# ---------------------------------------------------------------------------------------------------------------
# page
# ---------------------------------------------------------------------------------------------------------------


def test_page_renders_escaped_self_contained_html():
    cur = {
        "nightly_id": "20261024T010000Z",
        "mode": "reduced",
        "exit_code": EXIT_CHANGED,
        "wall_s": 12.5,
        "git": {"head": "0123456789abcdef", "dirty": True, "branch": "main"},
        "checks": {
            "hbt": {
                **_check(metrics={"n": 4, "t": 1.0}, noisy=["t"]),
                "title": "<b>hbt</b>",
                "wall_s": 3.0,
                "gates": [{"name": "g<1>", "ok": True, "detail": "a&b"}],
                "artifacts": {"log": "checks/hbt/log.txt"},
            },
            "nt-rc": {**_check("error", required=False), "reason": "no <wheel>", "wall_s": 1.0, "gates": []},
        },
    }
    prev = {
        **cur,
        "nightly_id": "20261023T010000Z",
        "exit_code": 0,
        "checks": {"hbt": {**_check(metrics={"n": 3, "t": 2.0}, noisy=["t"]), "gates": []}},
    }
    cur["diff"] = H.diff_runs(prev, cur)
    hist = {"runs": [prev, cur]}
    html = P.render(cur, hist, link_base="runs/20261024T010000Z/", runs_base="runs/", run_pages={"20261023T010000Z"})
    assert html.startswith("<!doctype html>") and "<title>queuetruth nightly</title>" in html
    assert "&lt;b&gt;hbt&lt;/b&gt;" in html and "<b>hbt</b>" not in html
    assert "g&lt;1&gt;" in html and "a&amp;b" in html and "no &lt;wheel&gt;" in html
    assert 'href="runs/20261024T010000Z/checks/hbt/log.txt"' in html
    assert "prefers-color-scheme: dark" in html and "--bg" in html
    assert "http://" not in html.replace('"http://www.w3.org', "") and "https://" not in html  # no external resource
    assert "exit 3" in html and "20261023T010000Z" in html
    assert html.count("<tr>") >= 6
    assert 'href="runs/20261023T010000Z/index.html"' in html  # history row of a run still on disk
    assert 'href="runs/20261024T010000Z/index.html"' not in html


def test_page_without_previous_run():
    run = {"nightly_id": "x", "mode": "full", "exit_code": 0, "checks": {}, "diff": H.diff_runs(None, {})}
    html = P.render(run, {"runs": [run]})
    assert "No previous full run" in html
