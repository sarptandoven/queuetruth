"""Recording runs: the switch, the default location, the decorator (pass-through, nesting, errors, failures that
must not fail the run), identities derived from specs and summaries, and the code version."""

from __future__ import annotations

import hashlib
import logging
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from queuetruth import api
from queuetruth import ledger as L
from queuetruth.errors import QtError
from queuetruth.ledger import _capture as cap
from queuetruth.schemas import models as m
from queuetruth.schemas.runid import canonical_json, run_id


def _records() -> list[L.LedgerRecord]:
    return L.list_records()


def _rr(kind: str, **kw: Any) -> L.LedgerRecord:
    return cap.run_record(kind, **kw).record


# ---- switch and location ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "auto"), ("", "auto"), ("off", "off"), ("0", "off"), ("FALSE", "off"), (" no ", "off"),
     ("disabled", "off"), ("on", "on"), ("1", "on"), ("Yes", "on"), ("maybe", "auto")],
)  # fmt: skip
def test_switch(monkeypatch: pytest.MonkeyPatch, value: str | None, expected: str) -> None:
    if value is None:
        monkeypatch.delenv("QT_LEDGER", raising=False)
    else:
        monkeypatch.setenv("QT_LEDGER", value)
    assert cap.switch() == expected


def test_the_service_never_records(monkeypatch: pytest.MonkeyPatch) -> None:
    assert cap.auto_enabled()
    monkeypatch.setitem(sys.modules, "queuetruth_service", types.ModuleType("queuetruth_service"))
    assert not cap.auto_enabled()  # even with QT_LEDGER_DIR set (the fixture) ...
    monkeypatch.setenv("QT_LEDGER", "on")
    assert not cap.auto_enabled()  # ... or forced on: one local file would mix tenants
    _fake_zoo({})
    assert not cap.default_ledger().exists()


def test_test_suites_do_not_record_unless_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under pytest a ledger is written only with QT_LEDGER_DIR (this suite's fixture) or QT_LEDGER=on, so the
    synthetic runs of a test suite never land in a research ledger."""
    import os

    assert os.environ.get("PYTEST_CURRENT_TEST") and cap.auto_enabled()
    monkeypatch.delenv("QT_LEDGER_DIR")
    assert not cap.auto_enabled()
    monkeypatch.setenv("QT_LEDGER", "on")
    assert cap.auto_enabled()
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    monkeypatch.delenv("QT_LEDGER")
    assert cap.auto_enabled()  # outside pytest, auto records
    monkeypatch.setenv("QT_LEDGER", "off")
    assert not cap.auto_enabled()


def test_default_location_is_the_project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QT_LEDGER_DIR")
    proj = tmp_path / "proj"
    (proj / "src" / "deep").mkdir(parents=True)
    (proj / "pyproject.toml").write_text("[project]\n")
    monkeypatch.chdir(proj / "src" / "deep")
    assert cap.project_root() == proj.resolve()
    led = cap.default_ledger()
    assert led.path == proj.resolve() / ".queuetruth" / "ledger" / "ledger.jsonl"
    L.register_variant("v", {"x": 1})
    gi = proj / ".queuetruth" / "ledger" / ".gitignore"
    assert gi.read_text().splitlines()[-1] == "*"  # kept out of git unless the user deletes it
    # outside any project the ledger is the per-user one, never the bare working directory
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.chdir(bare)
    marked = [
        d for d in (bare.resolve(), *bare.resolve().parents) if any((d / k).exists() for k in cap.PROJECT_MARKERS)
    ]
    assert cap.project_root() == (marked[0] if marked else None)
    if not marked:
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
        assert cap.default_ledger().directory == cap.user_data_dir() / "ledger"
        assert cap.user_data_dir().is_relative_to(home)
    # .queuetruth/ marks a root too, and an existing ledger directory keeps the user's choice about .gitignore
    other = tmp_path / "other"
    (other / ".queuetruth" / "ledger").mkdir(parents=True)
    (other / "a").mkdir()
    monkeypatch.chdir(other / "a")
    assert cap.project_root() == other.resolve()
    L.register_variant("v", {"x": 1})
    assert not (other / ".queuetruth" / "ledger" / ".gitignore").exists()


def test_qt_ledger_dir_wins(ledger_dir: Path) -> None:
    assert cap.default_ledger().path == ledger_dir / "ledger.jsonl"
    assert cap.as_ledger(ledger_dir / "x.jsonl").path == ledger_dir / "x.jsonl"


def test_study_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    assert cap.current_study() == "default"
    monkeypatch.setenv("QT_LEDGER_STUDY", "env-study")
    assert cap.current_study() == "env-study"
    with L.study("grid.1"):
        assert cap.current_study() == "grid.1"
        with L.study("inner"):
            assert cap.current_study() == "inner"
        assert cap.current_study("explicit") == "explicit"
    assert cap.current_study() == "env-study"
    with pytest.raises(QtError, match="QT-CONFIG-001"), L.study("no spaces"):
        pass


# ---- code version -------------------------------------------------------------------------------------------------


def test_code_version() -> None:
    from queuetruth._version import __version__
    from queuetruth.schemas.arrow import ENGINE_VERSION

    c = cap.code_version("my-tag")
    assert (c.queuetruth, c.engine, c.user) == (__version__, ENGINE_VERSION, "my-tag")


def test_project_git_is_the_callers_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "strategy"
    (root / "src").mkdir(parents=True)
    _git(root, "c" * 40)
    monkeypatch.chdir(root / "src")
    assert cap.project_git() == "c" * 40 and cap.code_version().project_git == "c" * 40
    monkeypatch.chdir(tmp_path)
    if not any((d / ".git").exists() for d in (tmp_path.resolve(), *tmp_path.resolve().parents)):
        assert cap.code_version().project_git is None


def _git(root: Path, sha: str, *, worktree: bool = False, packed: bool = False) -> None:
    gitdir = root / ".git"
    if worktree:
        common = root.parent / "common.git"
        gitdir = common / "worktrees" / root.name
        gitdir.mkdir(parents=True)
        (root / ".git").write_text(f"gitdir: {gitdir}\n")
        (gitdir / "commondir").write_text("../..\n")
        refs = common
    else:
        gitdir.mkdir()
        refs = gitdir
    (gitdir / "HEAD").write_text("ref: refs/heads/main\n")
    if packed:
        (refs / "packed-refs").write_text(f"# pack-refs with: peeled\n{sha} refs/heads/main\n")
    else:
        (refs / "refs" / "heads").mkdir(parents=True)
        (refs / "refs" / "heads" / "main").write_text(sha + "\n")


def test_git_head_forms(tmp_path: Path) -> None:
    sha = "a" * 40
    for i, kw in enumerate(({}, {"worktree": True}, {"packed": True}, {"worktree": True, "packed": True})):
        root = tmp_path / f"r{i}"
        root.mkdir()
        _git(root, sha, **kw)
        assert cap.git_head(root) == sha, kw
    detached = tmp_path / "det"
    (detached / ".git").mkdir(parents=True)
    (detached / ".git" / "HEAD").write_text("b" * 40 + "\n")
    assert cap.git_head(detached) == "b" * 40
    none = tmp_path / "none"
    none.mkdir()
    assert cap.git_head(none) is None
    bad = tmp_path / "bad"
    (bad / ".git").mkdir(parents=True)
    (bad / ".git" / "HEAD").write_text("ref: refs/heads/gone\n")
    assert cap.git_head(bad) is None
    junk = tmp_path / "junk"
    junk.mkdir()
    (junk / ".git").write_text("not a gitdir line")
    assert cap.git_head(junk) is None


# ---- identities ---------------------------------------------------------------------------------------------------


def test_spec_hash_ignores_execution_fields_data_and_defaulted_1_1_fields() -> None:
    a = {"dataset": {"path": "a"}, "shadows": {"path": "s.parquet"}, "seed": 1, "out": "o1", "threads": 4}
    b = {"dataset": {"path": "b"}, "shadows": {"upload_id": "u"}, "seed": 1, "out": "o2", "threads": 1}
    assert cap.spec_hash("zoo", a) == cap.spec_hash("zoo", b)
    assert cap.spec_hash("zoo", a) != cap.spec_hash("zoo", {**a, "seed": 2})
    assert cap.spec_hash("zoo", a) != cap.spec_hash("calibrate", a)
    z = {"write_fills": True, "amends": None, "exclude_order_ids": []}  # 1.1 defaults of ZooSpec
    assert cap.spec_hash("zoo", z) == cap.spec_hash("zoo", {"write_fills": True})
    assert len(cap.spec_hash("zoo", a)) == 16


def test_inputs_and_dataset_hash(tmp_path: Path) -> None:
    ds = tmp_path / "ds"
    ds.mkdir()
    (ds / "manifest.json").write_text('{"dataset_id": "' + "d" * 64 + '"}')
    f = tmp_path / "s.parquet"
    f.write_bytes(b"shadow bytes")
    spec = {"dataset": {"path": str(ds)}, "nested": [{"shadows": {"path": str(f)}}], "out": {"path": "ignored"}}
    inputs, complete = cap.inputs_of(spec)
    assert complete
    assert inputs == {"dataset": "d" * 64, "nested.0.shadows": hashlib.sha256(b"shadow bytes").hexdigest()}
    assert cap.dataset_hash(inputs) == hashlib.sha256(canonical_json(inputs).encode()).hexdigest()
    assert cap.dataset_hash({}) is None
    inputs, complete = cap.inputs_of({"dataset": {"path": str(tmp_path / "missing")}})
    assert (inputs, complete) == ({}, False)
    # the file hash is cached by (path, size, mtime) and recomputed when the file changes
    f.write_bytes(b"changed!!!!!!!!")
    assert cap.inputs_of({"s": {"path": str(f)}})[0]["s"] == hashlib.sha256(b"changed!!!!!!!!").hexdigest()


def _zoo_summary(spec: dict[str, Any], rid: str) -> m.ZooSummary:
    return m.ZooSummary(
        run_id=rid, spec=spec, n_shadows=7, models=["a", "b"], leaderboard=[], files=[], warnings=[], n_leaderboard=5
    )


def test_run_record_from_a_summary(tmp_path: Path) -> None:
    ds = tmp_path / "ds"
    ds.mkdir()
    (ds / "manifest.json").write_text('{"schema": "qt.dataset_manifest", "dataset_id": "' + "e" * 64 + '"}')
    echo = {"dataset": {"path": str(ds)}, "seed": 3}
    rec = _rr("zoo", spec={"ignored": True}, summary=_zoo_summary(echo, "0123456789abcdef"))
    assert (rec.kind, rec.status, rec.run_id, rec.trial_id) == ("zoo", "ok", "0123456789abcdef", "0123456789abcdef")
    assert rec.inputs == {"dataset": "e" * 64} and rec.spec_hash == cap.spec_hash("zoo", echo)
    assert rec.metrics == {"n_shadows": 7, "n_leaderboard": 5, "n_excluded_hbt_312": 0, "n_models": 2}
    assert rec.study == "default" and rec.error is None
    # without a reported run_id (shadows sample) it is recomputed from the echo exactly as SPEC 7.3 defines it
    from queuetruth.schemas.arrow import ENGINE_VERSION

    rec = _rr("shadows-sample", summary={"spec": echo, "n": 4})
    assert rec.run_id == run_id(echo, ENGINE_VERSION, lambda r: "e" * 64) and rec.metrics == {"n": 4}
    assert rec.run_id == api.run_id(echo)  # computed by the Rust core, which defines this run_id
    # an unresolvable reference leaves the run id unknown rather than wrong
    rec = _rr("shadows-sample", summary={"spec": {"dataset": {"path": str(tmp_path / "gone")}}, "n": 1})
    assert rec.run_id is None and rec.trial_id is None and rec.inputs == {}


def test_inputs_resolve_as_each_kinds_run_id_does(tmp_path: Path) -> None:
    """The Rust core (zoo, calibrate, shadows sample) and the Python layer (score, calibrate-live, fit-hazard)
    identify some references differently; each record uses the rule of the code that computed its run_id."""
    from queuetruth.score._io import resolve_ref

    plain = tmp_path / "plain"  # a manifest that is not a qt.dataset_manifest
    plain.mkdir()
    (plain / "manifest.json").write_text('{"dataset_id": "' + "f" * 64 + '"}')
    bare = tmp_path / "bare"  # a directory without a manifest
    bare.mkdir()
    (bare / "a.txt").write_text("a")
    by_id = {"dataset_id": "1" * 64}
    spec = {"a": {"path": str(plain)}, "b": by_id}
    core, ok = cap.inputs_of(spec, "zoo")
    assert ok and core == {"a": hashlib.sha256((plain / "manifest.json").read_bytes()).hexdigest(), "b": "1" * 64}
    py, ok = cap.inputs_of(spec, "score")
    assert ok and py == {"a": "f" * 64, "b": resolve_ref(by_id)}
    assert cap.inputs_of({"a": {"path": str(bare)}}, "zoo") == ({}, False)  # the core refuses it
    assert cap.inputs_of({"a": {"path": str(bare)}}, "score")[0]["a"] == resolve_ref({"path": str(bare)})
    # calibrate latency is computed in Python
    assert cap.inputs_of({"kind": "latency", **spec}, "calibrate")[0] == py
    # a dataset referenced by id: the core's run_id, and the --dataset filter, both use the id itself
    rec = _rr("shadows-sample", summary={"spec": {"dataset": by_id, "n": 3}, "n": 3})
    assert rec.inputs == {"dataset": "1" * 64} and rec.run_id == api.run_id({"dataset": by_id, "n": 3})


def test_run_record_metrics_by_kind() -> None:
    cal = {"run_id": "1" * 16, "family": "power3", "kind": "alloc", "estimate": 0.3, "n_events": 9, "n_shadows": 2,
           "trust": {"verdict": "refused"}, "spec": {}}  # fmt: skip
    rec = _rr("calibrate", summary=cal)
    assert rec.name == "power3" and rec.metrics["trust_verdict"] == "refused" and rec.metrics["estimate"] == 0.3
    assert rec.status == "ok" and rec.trial_id == "1" * 16  # a refused trust verdict is a result, not an error
    held = {"held_out": {"fitted": {"brier": {"1": 0.1, "2": 0.3, "3": None}}},
            "baseline_held_out": {"default": {"brier": {"1": 0.2, "2": 0.4}}}}  # fmt: skip
    rec = _rr("calibrate", summary={**cal, **held})
    assert rec.metrics["held_out_brier_mean"] == pytest.approx(0.2)
    assert rec.metrics["baseline_held_out_brier_mean"] == pytest.approx(0.3)
    hz = {"model_id": "hazard_gbdt:" + "2" * 16, "n_rows_train": 10, "scores_test": {"brier": 0.1, "auc": None,
          "nested": {"x": 1}}, "spec": {}}  # fmt: skip
    rec = _rr("fit-hazard", summary=hz)
    assert rec.run_id == "2" * 16 and rec.metrics["test_brier"] == 0.1 and "test_nested" not in rec.metrics
    sc = {"run_id": "3" * 16, "n_orders": 5, "n_matched": 4, "verdict": {"signed_bias": float("nan"),
          "gates_passed": True, "headline": "h" * 1000}, "spec": {}}  # fmt: skip
    rec = _rr("score", summary=sc)
    assert rec.metrics["signed_bias"] is None and len(rec.metrics["headline"]) == cap.MAX_METRIC_TEXT
    from queuetruth.schemas.arrow import ENGINE_VERSION

    rec = _rr("zoo", summary={"run_id": "NOT-HEX", "spec": {}})
    assert rec.run_id == run_id({}, ENGINE_VERSION, str)  # an invalid reported id is recomputed from the echo
    rec = _rr("calibrate", summary={"family": "has space", "n_events": 1, "estimate": 0.1, "spec": {}})
    assert rec.name is None
    rec = _rr("zoo", summary=object())  # anything else: no fields, still a record
    assert rec.metrics == {} and rec.run_id is None
    with pytest.raises(QtError, match="QT-CONFIG-001"):
        cap.run_record("replay")


def test_zoo_records_its_own_selection() -> None:
    board = [
        {"model_id": "a", "brier": {"1": 0.2, "2": 0.4}},
        {"model_id": "b", "brier": {"1": 0.1, "2": None}},
        {"model_id": "c", "brier": {"1": None}},
    ]
    rec = _rr("zoo", summary={"run_id": "4" * 16, "spec": {}, "models": ["a", "b", "c"], "leaderboard": board})
    m_ = rec.metrics
    assert m_["n_models"] == 3 and m_["brier_mean:a"] == pytest.approx(0.3) and m_["brier_mean:b"] == 0.1
    assert "brier_mean:c" not in m_ and m_["best_model"] == "b" and m_["best_margin"] == pytest.approx(0.2)


def test_results_the_cli_refuses_are_failed_runs() -> None:
    """`score` that matched no order and a calibration that fitted no event return from the API but are errors
    (exit 2) in the CLI; the ledger records them as failed runs, with no trial."""
    from queuetruth import cli

    sc = {"run_id": "5" * 16, "n_orders": 4, "n_matched": 0, "warnings": ["no instrument"], "spec": {}}
    rec = _rr("score", summary=sc)
    assert (rec.status, rec.trial_id, rec.run_id) == ("error", None, "5" * 16)
    assert rec.error is not None and rec.error.code == "QT-INPUT-002" and "NOT SCORED" in rec.error.message
    assert rec.error.message == cli._not_scored(sc).message
    for cal in (
        {"run_id": "6" * 16, "kind": "alloc", "family": "power3", "n_events": 0, "estimate": None, "spec": {}},
        {"run_id": "6" * 16, "kind": "latency", "family": "x", "latency": {"entry_ns": {"n": 0}}, "spec": {}},
    ):
        for kind in ("calibrate", "calibrate-live"):
            rec = _rr(kind, summary=cal)
            assert (rec.status, rec.trial_id, rec.error.code) == ("error", None, "QT-INPUT-002"), (kind, cal)
    assert _rr("score", summary={**sc, "n_matched": 1}).trial_id == "5" * 16


def test_error_record_spec_hash_matches_the_ok_run_of_the_same_spec() -> None:
    from queuetruth.schemas import specs as s

    given = {"dataset": {"path": "ds"}, "shadows": {"path": "x.parquet"}, "seed": 2, "out": "o"}
    echo = s.ZooSpec.model_validate(given).model_dump(mode="json", by_alias=True)
    echo.pop("out", None)
    ok = _rr("zoo", summary=_zoo_summary(echo, "a" * 16))
    err = _rr("zoo", spec=given, error=QtError("QT-INPUT-001", "missing"))
    assert err.spec_hash == ok.spec_hash and err.trial_id is None and err.run_id is None and err.inputs == {}
    assert err.error is not None and (err.error.code, err.error.message) == ("QT-INPUT-001", "missing")
    # options that are spec fields (score's keyword extras) fold into the spec
    sc = _rr("score", spec={"live": {"path": "l"}, "sim": {"kind": "exact"}, "dataset": {"path": "d"}},
                        options={"window_ns": 5, "not_a_field": 1}, error=ValueError("x"))  # fmt: skip
    sc2 = _rr("score", spec={"live": {"path": "l"}, "sim": {"kind": "exact"}, "dataset": {"path": "d"},
                         "window_ns": 5}, error=ValueError("x"))  # fmt: skip
    assert sc.spec_hash == sc2.spec_hash and sc.error.code == "QT-ENGINE-001"
    assert _rr("zoo", spec="not a mapping", error=ValueError("x")).spec_hash is None
    assert _rr("zoo", spec={"bad": object()}, error=ValueError("x")).spec_hash is None


# ---- the decorator ------------------------------------------------------------------------------------------------


@cap.recorded("zoo")
def _fake_zoo(spec: dict[str, Any], *, extra: int = 0) -> m.ZooSummary:
    """Fake entry point."""
    if spec.get("fail"):
        raise QtError("QT-INPUT-002", "bad input")
    if spec.get("crash"):
        raise RuntimeError("bug")
    return _zoo_summary({"seed": spec.get("seed", 0)}, spec.get("rid", "f" * 16))


@cap.recorded("score")
def _fake_score(spec: dict[str, Any]) -> m.ZooSummary:
    _fake_zoo({"rid": "e" * 16})  # nested: part of this trial
    return _zoo_summary({"x": 1}, "d" * 16)


def test_decorator_passes_everything_through_and_records() -> None:
    assert _fake_zoo.__name__ == "_fake_zoo" and _fake_zoo.__doc__ == "Fake entry point."
    assert _fake_zoo.__wrapped__ is not None and _fake_zoo.__qt_ledger_kind__ == "zoo"
    out = _fake_zoo({"seed": 1}, extra=2)
    assert isinstance(out, m.ZooSummary) and out.run_id == "f" * 16
    with pytest.raises(QtError) as e:
        _fake_zoo({"fail": True})
    assert e.value.code == "QT-INPUT-002"
    with pytest.raises(RuntimeError, match="bug"):
        _fake_zoo({"crash": True})
    recs = _records()
    assert [(r.kind, r.status) for r in recs] == [("zoo", "ok"), ("zoo", "error"), ("zoo", "error")]
    assert [r.error.code for r in recs[1:]] == ["QT-INPUT-002", "QT-ENGINE-001"]
    assert recs[0].started_at_ns is not None and recs[0].started_at_ns <= recs[0].recorded_at_ns


def test_nested_entry_points_are_one_trial() -> None:
    _fake_score({})
    assert [(r.kind, r.run_id) for r in _records()] == [("score", "d" * 16)]
    _fake_zoo({"rid": "c" * 16})  # the guard is reset afterwards
    assert [r.kind for r in _records()] == ["score", "zoo"]


def test_keyboard_interrupt_is_not_recorded() -> None:
    @cap.recorded("zoo")
    def interrupted(spec: Any) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        interrupted({})
    assert not cap.default_ledger().exists()
    _fake_zoo({})
    assert len(_records()) == 1  # the guard was reset


def test_off_switch_records_nothing(monkeypatch: pytest.MonkeyPatch, ledger_dir: Path) -> None:
    monkeypatch.setenv("QT_LEDGER", "off")
    _fake_zoo({})
    with pytest.raises(QtError):
        _fake_zoo({"fail": True})
    assert not ledger_dir.exists()


def test_a_recording_failure_never_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    monkeypatch.setenv("QT_LEDGER_DIR", str(blocker / "sub"))
    with caplog.at_level(logging.WARNING, logger="queuetruth.ledger"):
        out = _fake_zoo({"seed": 5})
    assert out.run_id == "f" * 16
    assert any("zoo run not recorded: QT-LEDGER-002" in r.getMessage() for r in caplog.records)
    caplog.clear()
    monkeypatch.setenv("QT_LEDGER_DIR", str(tmp_path / "ok"))
    monkeypatch.setenv("QT_LEDGER_STUDY", "bad study")
    with caplog.at_level(logging.WARNING, logger="queuetruth.ledger"):
        assert _fake_zoo({}).run_id == "f" * 16
    assert any("QT-CONFIG-001" in r.getMessage() for r in caplog.records)
    caplog.clear()
    monkeypatch.delenv("QT_LEDGER_STUDY")

    def boom(*a: Any, **k: Any) -> Any:
        raise ZeroDivisionError("unexpected")

    monkeypatch.setattr(cap, "run_record", boom)
    with caplog.at_level(logging.WARNING, logger="queuetruth.ledger"):
        assert _fake_zoo({}).run_id == "f" * 16
        with pytest.raises(QtError, match="QT-INPUT-002"):  # the run's own error, not the ledger's
            _fake_zoo({"fail": True})
    assert sum("ZeroDivisionError" in r.getMessage() for r in caplog.records) == 2


def test_unknown_kind_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="unknown kind"):
        cap.recorded("replay")


def test_record_public_api(tmp_path: Path) -> None:
    rec = L.record("zoo", summary=_zoo_summary({"seed": 1}, "9" * 16), note="by hand", study="manual")
    assert rec is not None and rec.seq == 1 and rec.note == "by hand" and rec.study == "manual"
    other = L.record("calibrate", summary={"run_id": "8" * 16, "spec": {}}, ledger=tmp_path / "other")
    assert other is not None and L.Ledger(tmp_path / "other").records()[-1] == other


def test_api_entry_points_are_wrapped() -> None:
    kinds = {
        api.sample_shadows: "shadows-sample",
        api.run_zoo: "zoo",
        api.calibrate: "calibrate",
        api.calibrate_live: "calibrate-live",
        api.fit_hazard: "fit-hazard",
        api.score: "score",
    }
    for fn, kind in kinds.items():
        assert fn.__qt_ledger_kind__ == kind and fn.__wrapped__.__name__ == fn.__name__
    assert not hasattr(api.replay, "__qt_ledger_kind__") and not hasattr(api.ingest, "__qt_ledger_kind__")


def test_api_error_runs_are_recorded_without_the_core(tmp_path: Path) -> None:
    """A spec that fails validation in the API itself (no engine work) is recorded as a failed run."""
    with pytest.raises(QtError) as e:
        api.fit_hazard({"features": {"path": str(tmp_path / "f.parquet")}, "grid_ns": "not a list"})
    assert e.value.code == "QT-CONFIG-001"
    (rec,) = _records()
    assert (rec.kind, rec.status, rec.error.code) == ("fit-hazard", "error", "QT-CONFIG-001")
    assert rec.spec_hash is not None  # the raw spec is hashed when it does not validate


def test_native_cli_hook() -> None:
    calls = []

    def runner(cmd: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        calls.append(cmd)
        if cmd["args"].get("n") == 0:
            raise QtError("QT-CONFIG-001", "n: must be > 0")
        n = cmd["args"].get("n")
        return {"schema": "qt.shadow_sample_summary", "spec": {"n": n}, "n": n}, False

    cmd = {"path": ["shadows", "sample"], "args": {"n": 3, "_cli": {"x": 1}}, "implementation": "native"}
    assert cap.run_native(cmd, runner) == ({"schema": "qt.shadow_sample_summary", "spec": {"n": 3}, "n": 3}, False)
    with pytest.raises(QtError, match="QT-CONFIG-001"):
        cap.run_native({**cmd, "args": {"n": 0}}, runner)
    other = {"path": ["replay"], "args": {}, "implementation": "native"}
    assert cap.run_native(other, runner)[1] is False
    recs = _records()
    assert [(r.kind, r.status, r.metrics) for r in recs] == [
        ("shadows-sample", "ok", {"n": 3}),
        ("shadows-sample", "error", {}),
    ]
    assert len(calls) == 3 and calls[0] is cmd  # the command reaches the runner unchanged


def test_calibrate_live_echo_tag_is_not_part_of_the_spec() -> None:
    from queuetruth.schemas import specs as s

    given = {"dataset": {"path": "ds"}, "live": {"path": "live"}, "family": "power3"}
    echo = s.LiveCalibrationSpec.model_validate(given).model_dump(mode="json", by_alias=True)
    ok = _rr("calibrate-live", summary={"run_id": "7" * 16, "spec": {**echo, "calibration_source": "live"}})
    err = _rr("calibrate-live", spec=given, error=QtError("QT-DATA-001", "no orders"))
    assert ok.spec_hash == err.spec_hash == cap.spec_hash("calibrate-live", echo)
